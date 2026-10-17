from datetime import timedelta
import logging

from homeassistant.components.climate import (
    ATTR_FAN_MODE,
    ATTR_FAN_MODES,
    ATTR_HVAC_ACTION,
    ATTR_HVAC_MODE,
    ATTR_SWING_MODE,
    ATTR_SWING_MODES,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_FAN_MODE,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_SWING_MODE,
    SERVICE_SET_TEMPERATURE,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    ATTR_TEMPERATURE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant, State, callback

from ..hvac_controller.wrapped_climate_controller import WrappedClimateHvacController
from ..hvac_device.cooler_device import CoolerDevice
from ..managers.environment_manager import EnvironmentManager
from ..managers.feature_manager import FeatureManager
from ..managers.hvac_power_manager import HvacPowerManager
from ..managers.opening_manager import OpeningManager

_LOGGER = logging.getLogger(__name__)


class WrappedClimateDevice(CoolerDevice):
    """Cooler device that delegates to a real ``climate`` entity.

    Instead of toggling a switch on/off, this device commands a wrapped
    ``climate`` entity (e.g. a Gree split AC) via ``climate.set_hvac_mode`` and
    ``climate.set_temperature``, and reads its running state / action back from
    the wrapped entity. The thermostat still owns the control decision (when to
    cool) based on the room sensor; the wrapped entity executes it.

    Fan speed and swing capabilities are detected from the wrapped entity so
    they can be passed through (see ``async_set_fan_mode`` /
    ``async_set_swing_mode``).
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

        # Replace the switch-based controller with one that understands a
        # climate entity's state model.
        self.hvac_controller = WrappedClimateHvacController(
            hass,
            entity_id,
            min_cycle_duration,
            environment,
            openings,
            self.async_turn_on,
            self.async_turn_off,
        )

        self._last_target_temp = None

        # Capabilities detected from the wrapped entity.
        self._supports_fan_mode = False
        self._fan_modes: list[str] = []
        self._supports_swing_mode = False
        self._swing_modes: list[str] = []
        self._supports_target_temperature = False

        self._detect_capabilities()

    # ----- capability detection ------------------------------------------------

    def _detect_capabilities(self, state: State | None = None) -> None:
        """Read fan/swing/temperature capabilities from the wrapped entity.

        When the wrapped entity is unavailable/unknown its attributes are empty,
        so we keep the last-known capabilities instead of wiping them. They are
        re-read once the entity reports a real state again (e.g. it was offline
        at Home Assistant startup and later came online).
        """
        if state is None:
            state = self.hass.states.get(self.entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return

        features = state.attributes.get(ATTR_SUPPORTED_FEATURES) or 0

        self._supports_target_temperature = bool(
            features & ClimateEntityFeature.TARGET_TEMPERATURE
        )

        if features & ClimateEntityFeature.FAN_MODE:
            self._supports_fan_mode = True
            self._fan_modes = list(state.attributes.get(ATTR_FAN_MODES) or [])
        else:
            self._supports_fan_mode = False
            self._fan_modes = []

        if features & ClimateEntityFeature.SWING_MODE:
            self._supports_swing_mode = True
            self._swing_modes = list(state.attributes.get(ATTR_SWING_MODES) or [])
        else:
            self._supports_swing_mode = False
            self._swing_modes = []

        _LOGGER.debug(
            "Wrapped climate %s capabilities: fan=%s %s, swing=%s %s, target_temp=%s",
            self.entity_id,
            self._supports_fan_mode,
            self._fan_modes,
            self._supports_swing_mode,
            self._swing_modes,
            self._supports_target_temperature,
        )

    @callback
    def on_entity_state_changed(self, entity_id: str, new_state: State) -> None:
        """Refresh capabilities when the wrapped entity reports a new state."""
        if entity_id == self.entity_id and new_state is not None:
            self._detect_capabilities(new_state)

    @property
    def supports_fan_mode(self) -> bool:
        return self._supports_fan_mode

    @property
    def fan_modes(self) -> list[str]:
        return self._fan_modes

    @property
    def current_fan_mode(self) -> str | None:
        state = self._entity_state
        return state.attributes.get(ATTR_FAN_MODE) if state else None

    def restore_fan_mode(self, fan_mode: str) -> None:
        """No-op: the wrapped climate entity restores its own fan mode.

        Provided for duck-typing compatibility with ``FanDevice`` so the feature
        manager's fan-mode restore path works when this device is used as the
        fan device.
        """
        _LOGGER.debug(
            "Skipping fan mode restore for wrapped climate %s (entity restores "
            "its own state)",
            self.entity_id,
        )

    def redetect_capabilities(self, entity_id: str, new_state: State) -> bool:
        """No-op: on_entity_state_changed already refreshes our capabilities.

        Provided for duck-typing compatibility with ``FanDevice``, which the
        factory may register as the fan device in our place. Returning False
        keeps the caller from setting the FAN_MODE bit here - the thermostat's
        _refresh_passthrough_support_flags already adds it (along with
        SWING_MODE) straight after forwarding the state change to us.
        """
        return False

    @property
    def supports_swing_mode(self) -> bool:
        return self._supports_swing_mode

    @property
    def swing_modes(self) -> list[str]:
        return self._swing_modes

    @property
    def current_swing_mode(self) -> str | None:
        state = self._entity_state
        return state.attributes.get(ATTR_SWING_MODE) if state else None

    # ----- state / feedback ----------------------------------------------------

    @property
    def is_on(self) -> bool:
        state = self._entity_state
        return state is not None and state.state not in (
            HVACMode.OFF,
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        )

    @property
    def hvac_action(self) -> HVACAction:
        if self.hvac_mode == HVACMode.OFF:
            return HVACAction.OFF

        # An offline AC isn't doing anything; don't claim it's cooling.
        state = self._entity_state
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return HVACAction.OFF

        # Prefer the wrapped entity's own reported action when available.
        action = state.attributes.get(ATTR_HVAC_ACTION)
        if action is not None:
            return action

        if self.is_active:
            return HVACAction.COOLING
        return HVACAction.IDLE

    # ----- commands ------------------------------------------------------------

    async def _async_turn_on_entity(self) -> None:
        """Command the wrapped climate entity to cool."""
        if not self._wrapped_available():
            _LOGGER.debug(
                "Skipping set_hvac_mode for unavailable climate %s", self.entity_id
            )
            return

        await self._async_set_climate_hvac_mode(HVACMode.COOL)
        await self._async_sync_target_temperature()

    async def _async_turn_off_entity(self) -> None:
        """Command the wrapped climate entity to turn off."""
        if not self._wrapped_available():
            _LOGGER.debug(
                "Skipping set_hvac_mode(off) for unavailable climate %s",
                self.entity_id,
            )
            return

        await self._async_set_climate_hvac_mode(HVACMode.OFF)

    def _wrapped_available(self) -> bool:
        state = self.hass.states.get(self.entity_id) if self.entity_id else None
        return state is not None and state.state not in (
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        )

    async def _async_set_climate_hvac_mode(self, hvac_mode: HVACMode) -> None:
        _LOGGER.info(
            "Setting wrapped climate %s hvac mode to %s", self.entity_id, hvac_mode
        )
        try:
            await self.hass.services.async_call(
                CLIMATE_DOMAIN,
                SERVICE_SET_HVAC_MODE,
                {ATTR_ENTITY_ID: self.entity_id, ATTR_HVAC_MODE: hvac_mode},
                context=self._context,
                blocking=True,
            )
        except Exception as e:  # noqa: BLE001
            _LOGGER.error("Error setting hvac mode on %s. Error: %s", self.entity_id, e)

    async def _async_sync_target_temperature(self) -> None:
        """Pass the thermostat's target temperature through to the wrapped entity."""
        if not self._supports_target_temperature:
            return
        if not self._wrapped_available():
            _LOGGER.debug(
                "Skipping set_temperature for unavailable climate %s", self.entity_id
            )
            return

        target_temp = getattr(self.environment, self.target_env_attr, None)
        if target_temp is None or target_temp == self._last_target_temp:
            return

        _LOGGER.info(
            "Syncing target temperature %s to wrapped climate %s",
            target_temp,
            self.entity_id,
        )
        try:
            await self.hass.services.async_call(
                CLIMATE_DOMAIN,
                SERVICE_SET_TEMPERATURE,
                {ATTR_ENTITY_ID: self.entity_id, ATTR_TEMPERATURE: target_temp},
                context=self._context,
                blocking=True,
            )
            self._last_target_temp = target_temp
        except Exception as e:  # noqa: BLE001
            _LOGGER.error(
                "Error setting temperature on %s. Error: %s", self.entity_id, e
            )

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Pass a fan mode through to the wrapped climate entity."""
        if not self._supports_fan_mode:
            _LOGGER.warning(
                "Wrapped climate %s does not support fan mode control",
                self.entity_id,
            )
            return
        if self._fan_modes and fan_mode not in self._fan_modes:
            _LOGGER.warning(
                "Fan mode %s not supported by %s (supported: %s)",
                fan_mode,
                self.entity_id,
                self._fan_modes,
            )
            return
        if not self._wrapped_available():
            _LOGGER.debug(
                "Skipping set_fan_mode for unavailable climate %s", self.entity_id
            )
            return
        try:
            await self.hass.services.async_call(
                CLIMATE_DOMAIN,
                SERVICE_SET_FAN_MODE,
                {ATTR_ENTITY_ID: self.entity_id, ATTR_FAN_MODE: fan_mode},
                context=self._context,
                blocking=True,
            )
        except Exception as e:  # noqa: BLE001
            _LOGGER.error("Error setting fan mode on %s. Error: %s", self.entity_id, e)

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        """Pass a swing mode through to the wrapped climate entity."""
        if not self._supports_swing_mode:
            _LOGGER.warning(
                "Wrapped climate %s does not support swing mode control",
                self.entity_id,
            )
            return
        if self._swing_modes and swing_mode not in self._swing_modes:
            _LOGGER.warning(
                "Swing mode %s not supported by %s (supported: %s)",
                swing_mode,
                self.entity_id,
                self._swing_modes,
            )
            return
        if not self._wrapped_available():
            _LOGGER.debug(
                "Skipping set_swing_mode for unavailable climate %s", self.entity_id
            )
            return
        try:
            await self.hass.services.async_call(
                CLIMATE_DOMAIN,
                SERVICE_SET_SWING_MODE,
                {ATTR_ENTITY_ID: self.entity_id, ATTR_SWING_MODE: swing_mode},
                context=self._context,
                blocking=True,
            )
        except Exception as e:  # noqa: BLE001
            _LOGGER.error(
                "Error setting swing mode on %s. Error: %s", self.entity_id, e
            )

    def on_target_temperature_change(self, temperatures) -> None:
        """Re-sync the wrapped entity when the target temperature changes."""
        self._last_target_temp = None
        if self.is_active:
            self.hass.async_create_task(self._async_sync_target_temperature())
