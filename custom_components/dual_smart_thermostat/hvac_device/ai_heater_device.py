from datetime import timedelta
import logging

from homeassistant.components.climate import HVACMode
from homeassistant.core import HomeAssistant
import homeassistant.util.dt as dt_util

from ..control.thermal_learning import (
    DEFAULT_HEATING_POWER,
    HeatingPowerTracker,
    HeatLossTracker,
    heating_power_valve_fraction,
)
from ..control.tpi import TpiParams
from ..hvac_device.pwm_heater_device import PwmHeaterDevice
from ..managers.environment_manager import EnvironmentManager
from ..managers.feature_manager import FeatureManager
from ..managers.hvac_power_manager import HvacPowerManager
from ..managers.opening_manager import OpeningManager

_LOGGER = logging.getLogger(__name__)


class AiHeaterDevice(PwmHeaterDevice):
    """AI Time Based heating: PWM duty driven by a self-learned heating power.

    Extends the PWM heater with two EMA learners — one for the room's effective
    heating power (°C/min), one for its heat-loss rate — fed on every control
    pass. The per-cycle duty comes from the learned heating power via
    :func:`heating_power_valve_fraction` instead of the static TPI formula.
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
        initial_heating_power: float = DEFAULT_HEATING_POWER,
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
            cycle_duration,
            TpiParams(),  # unused by AI duty, kept for the base contract
        )
        self._heating_tracker = HeatingPowerTracker(heating_power=initial_heating_power)
        self._loss_tracker = HeatLossTracker()

    @property
    def heating_power(self) -> float:
        return self._heating_tracker.heating_power

    @property
    def heat_loss_rate(self) -> float:
        return self._loss_tracker.heat_loss_rate

    def restore_learned_state(
        self, heating_power: float | None, heat_loss: float | None
    ) -> None:
        """Restore learned values from persisted state after a restart."""
        if heating_power is not None:
            self._heating_tracker.heating_power = float(heating_power)
        if heat_loss is not None:
            self._loss_tracker.heat_loss_rate = float(heat_loss)
        _LOGGER.debug(
            "Restored AI learning for %s: heating_power=%s heat_loss=%s",
            self.entity_id,
            self._heating_tracker.heating_power,
            self._loss_tracker.heat_loss_rate,
        )

    def reset_learning(self) -> None:
        """Reset learned heating power / heat loss to defaults."""
        self._heating_tracker = HeatingPowerTracker()
        self._loss_tracker = HeatLossTracker()

    def _compute_duty(self) -> float:
        current = self.environment.cur_temp
        target = getattr(self.environment, self.target_env_attr, None)
        if current is None or target is None:
            return 0.0
        error = float(target) - float(current)
        return (
            heating_power_valve_fraction(error, self._heating_tracker.heating_power)
            * 100.0
        )

    async def async_control_hvac(self, time=None, force=False):
        self._feed_learners()
        await super().async_control_hvac(time, force)

    def _feed_learners(self) -> None:
        """Feed the current reading to both learners."""
        current = self.environment.cur_temp
        if current is None:
            return
        now = dt_util.utcnow()
        is_heating = self.is_on
        target = getattr(self.environment, self.target_env_attr, None)
        outdoor = self.environment.cur_outside_temp
        window_open = self.openings.any_opening_open(self.hvac_mode)

        self._heating_tracker.update(
            current, is_heating, now, target_temp=target, outdoor_temp=outdoor
        )
        self._loss_tracker.update(current, is_heating, now, window_open=window_open)
