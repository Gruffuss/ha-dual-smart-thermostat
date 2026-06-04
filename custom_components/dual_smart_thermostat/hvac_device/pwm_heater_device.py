from datetime import timedelta
import logging

from homeassistant.components.climate import HVACMode
from homeassistant.core import HomeAssistant

from ..control.tpi import TpiParams, compute_tpi_duty
from ..hvac_action_reason.hvac_action_reason import HVACActionReason
from ..hvac_controller.pwm_controller import PwmHeaterController
from ..hvac_device.heater_device import HeaterDevice
from ..managers.environment_manager import EnvironmentManager
from ..managers.feature_manager import FeatureManager
from ..managers.hvac_power_manager import HvacPowerManager
from ..managers.opening_manager import OpeningManager

_LOGGER = logging.getLogger(__name__)


class PwmHeaterDevice(HeaterDevice):
    """Heater device driven by a time-proportional (PWM) duty cycle.

    Instead of bang-bang hysteresis, the heater switch is pulsed on/off over a
    fixed cycle. The duty for each cycle comes from the TPI formula
    (:func:`compute_tpi_duty`). Mode/opening/floor handling mirrors the bang-bang
    controller: the cycle is stopped (switch off) when the thermostat is off, an
    opening is open, or the floor is too hot; the switch is forced fully on when
    the floor is too cold (floor protection).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entity_id: str,
        min_cycle_duration: timedelta,
        initial_hvac_mode: HVACMode,
        environment: EnvironmentManager,
        openings: OpeningManager,
        features: FeatureManager,
        hvac_power: HvacPowerManager,
        cycle_duration: timedelta,
        tpi_params: TpiParams,
    ) -> None:
        super().__init__(
            hass,
            entity_id,
            min_cycle_duration,
            initial_hvac_mode,
            environment,
            openings,
            features,
            hvac_power,
        )
        self._tpi_params = tpi_params
        self._pwm = PwmHeaterController(
            hass,
            entity_id,
            cycle_duration,
            min_cycle_duration,
            self._compute_duty,
            self.async_turn_on,
            self.async_turn_off,
        )
        # Ensure PWM timers are cancelled when the entity is removed.
        self.async_on_remove(self._pwm.cancel)

    @property
    def duty_cycle(self) -> float | None:
        """Last computed duty cycle percentage (None when not running)."""
        return self._pwm.last_duty

    async def async_set_hvac_mode(self, hvac_mode: HVACMode):
        """Set the mode, stopping the PWM cycle when turning off.

        The base implementation only turns the switch off (when active) on OFF
        and does not route through ``async_control_hvac``, so the PWM timers
        would keep running. Stop them explicitly here.
        """
        self.hvac_mode = hvac_mode if hvac_mode in self.hvac_modes else HVACMode.OFF

        if self.hvac_mode == HVACMode.OFF:
            await self._pwm.async_stop()
            self._hvac_action_reason = HVACActionReason.NONE
        else:
            await self.async_control_hvac(force=True)

    def _compute_duty(self) -> float:
        """Compute the heating duty for the current cycle from the TPI formula."""
        current = self.environment.cur_temp
        target = getattr(self.environment, self.target_env_attr, None)
        outdoor = self.environment.cur_outside_temp
        return compute_tpi_duty(current, target, outdoor, self._tpi_params)

    async def async_control_hvac(self, time=None, force=False):
        """Run proportional control, applying mode/opening/floor overrides."""
        self._set_self_active()

        if self._hvac_mode == HVACMode.OFF:
            await self._pwm.async_stop()
            self._hvac_action_reason = HVACActionReason.NONE
            return

        any_opening_open = self.openings.any_opening_open(self.hvac_mode)
        is_floor_hot = self.environment.is_floor_hot
        is_floor_cold = self.environment.is_floor_cold

        # Block heating: opening open or floor overheated (unless floor is cold,
        # which always wins for floor protection).
        if (any_opening_open or is_floor_hot) and not is_floor_cold:
            self._pwm.cancel()
            await self.async_turn_off()
            if is_floor_hot:
                self._hvac_action_reason = HVACActionReason.OVERHEAT
            if any_opening_open:
                self._hvac_action_reason = HVACActionReason.OPENING
            return

        # Floor protection: floor too cold -> force the valve fully open.
        if is_floor_cold:
            self._pwm.cancel()
            await self.async_turn_on()
            self._hvac_action_reason = HVACActionReason.LIMIT
            return

        # Don't start cycling until we have a current temperature, otherwise the
        # first (temperature-less) cycle would compute 0 duty and lock heating
        # out until the next cycle boundary.
        if self.environment.cur_temp is None:
            await self._pwm.async_stop()
            self._hvac_action_reason = HVACActionReason.NONE
            return

        # Normal proportional control.
        await self._pwm.async_start()

        duty = self._pwm.last_duty
        if duty is not None and duty <= 0.0:
            self._hvac_action_reason = HVACActionReason.TARGET_TEMP_REACHED
        else:
            self._hvac_action_reason = HVACActionReason.TARGET_TEMP_NOT_REACHED
