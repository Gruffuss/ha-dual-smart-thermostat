"""Sensor platform for dual_smart_thermostat.

Phase 0 of the Auto Mode roadmap (#563): exposes each climate entity's
``hvac_action_reason`` value as a diagnostic enum sensor entity. The sensor
is dual-exposed alongside the existing (deprecated) climate state attribute.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
import logging
import math

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
import homeassistant.util.dt as dt_util

from .const import (
    CONF_HEATER_CONTROL_MODE,
    DOMAIN,
    SET_HVAC_ACTION_REASON_SENSOR_SIGNAL,
    SET_PREDICTIVE_SENSOR_SIGNAL,
    HeaterControlMode,
)
from .hvac_action_reason.hvac_action_reason import HVACActionReason

_LOGGER = logging.getLogger(__name__)

# HVACActionReason.NONE is an empty string — Home Assistant's translation
# validator rejects empty keys, so the sensor surfaces "none" as the
# stable, translatable state value for that case.
STATE_NONE = "none"


def _build_options() -> tuple[str, ...]:
    values = {v.value or STATE_NONE for v in HVACActionReason}
    return tuple(sorted(values))


_OPTIONS: tuple[str, ...] = _build_options()
_OPTIONS_SET: frozenset[str] = frozenset(_OPTIONS)

PREDICTIVE_SENSOR_KEYS = (
    "forecast_30",
    "forecast_60",
    "forecast_120",
    "forecast_180",
    "forecast_240",
    "mae_60",
    "mae_120",
    "persistence_mae_120",
    "mode",
    "alpha_rel_sd",
    "slab",
    "tau",
    "planned_on_minutes",
    "validations_120_independent",
    "learning_blocks",
)


class PredictiveSensor(RestoreSensor):
    """One diagnostic value from the heater's isolated minute publication."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, sensor_key, name, key):
        self._sensor_key = sensor_key
        self._key = key
        self._attr_unique_id = f"{sensor_key}_predictive_{key}"
        self._attr_name = f"{name} Predictive {key.replace('_', ' ').title()}"
        self._attr_native_value = None
        self._last_write_error = None
        if key == "mode":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = ["fallback", "predictive"]
        else:
            self._attr_state_class = SensorStateClass.MEASUREMENT
        if key.startswith(("forecast_", "mae_", "persistence_mae_")):
            self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        if key.startswith("forecast_"):
            self._attr_device_class = SensorDeviceClass.TEMPERATURE
        if key in ("tau", "planned_on_minutes"):
            self._attr_native_unit_of_measurement = UnitOfTime.MINUTES

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is not None:
            value = last.native_value
            if self._key == "mode":
                if value in self._attr_options:
                    self._attr_native_value = value
            elif isinstance(value, (int, float)) and math.isfinite(value):
                self._attr_native_value = value
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SET_PREDICTIVE_SENSOR_SIGNAL.format(self._sensor_key),
                self._update_value,
            )
        )

    @callback
    def _update_value(self, values):
        # Dispatcher listeners run as separate HA jobs, outside the publisher's
        # exception handler. Isolate failures here as well as at the sender.
        try:
            self._attr_native_value = values.get(self._key)
            if self._key == "mode":
                self._attr_extra_state_attributes = {
                    "demoted_until": values.get("demoted_until")
                }
            self.async_write_ha_state()
        except Exception:
            now = dt_util.utcnow()
            if (
                self._last_write_error is None
                or now - self._last_write_error >= timedelta(seconds=600)
            ):
                self._last_write_error = now
                _LOGGER.exception(
                    "Predictive sensor write failed for %s", self.entity_id
                )


def _sensors(sensor_key, name, config):
    sensors = [HvacActionReasonSensor(sensor_key=sensor_key, name=name)]
    if config.get(CONF_HEATER_CONTROL_MODE) == HeaterControlMode.PREDICTIVE:
        sensors.extend(
            PredictiveSensor(sensor_key, name, key) for key in PREDICTIVE_SENSOR_KEYS
        )
    return sensors


class HvacActionReasonSensor(SensorEntity, RestoreEntity):
    """Diagnostic enum sensor that mirrors a climate's hvac_action_reason."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_translation_key = "hvac_action_reason"

    def __init__(self, sensor_key: str, name: str) -> None:
        """Initialise the sensor."""
        self._sensor_key = sensor_key
        self._attr_name = f"{name} HVAC Action Reason"
        self._attr_unique_id = f"{sensor_key}_hvac_action_reason"
        self._attr_options = _OPTIONS
        self._attr_native_value = STATE_NONE
        self._remove_signal: Callable[[], None] | None = None

    async def async_added_to_hass(self) -> None:
        """Restore previous state (if any) and subscribe to the mirror signal."""
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state in _OPTIONS_SET:
            self._attr_native_value = last_state.state
        else:
            if last_state is not None:
                _LOGGER.debug(
                    "Ignoring unknown restored state %s for %s; defaulting to none",
                    last_state.state,
                    self.entity_id,
                )
            self._attr_native_value = STATE_NONE

        self._remove_signal = async_dispatcher_connect(
            self.hass,
            SET_HVAC_ACTION_REASON_SENSOR_SIGNAL.format(self._sensor_key),
            self._handle_reason_update,
        )

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from the mirror signal."""
        if self._remove_signal is not None:
            self._remove_signal()
            self._remove_signal = None
        await super().async_will_remove_from_hass()

    @callback
    def _handle_reason_update(self, reason) -> None:
        """Update native_value from a dispatched reason; ignore invalid values."""
        raw = str(reason) if reason is not None else HVACActionReason.NONE
        value = raw or STATE_NONE

        if value not in _OPTIONS_SET:
            _LOGGER.warning(
                "Invalid hvac_action_reason %s for %s; ignoring",
                value,
                self.entity_id,
            )
            return

        if value == self._attr_native_value:
            return

        self._attr_native_value = value
        self.async_write_ha_state()


# Home Assistant's platform API requires ``async def`` for both setup entry
# points (HA awaits the returned coroutines). Without an actual ``await`` in
# the body, SonarCloud flags python:S7503 — suppressed explicitly because
# dropping ``async`` would break the HA contract.
async def async_setup_entry(  # NOSONAR python:S7503 - HA platform API
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the companion action-reason sensor for a config entry."""
    config = {**config_entry.data, **config_entry.options}
    name = config.get(CONF_NAME, "dual_smart_thermostat")
    sensor_key = config_entry.entry_id

    if config.get(CONF_HEATER_CONTROL_MODE) != HeaterControlMode.PREDICTIVE:
        registry = er.async_get(hass)
        for key in PREDICTIVE_SENSOR_KEYS:
            entity_id = registry.async_get_entity_id(
                "sensor", DOMAIN, f"{sensor_key}_predictive_{key}"
            )
            if entity_id is not None:
                registry.async_remove(entity_id)

    async_add_entities(_sensors(sensor_key, name, config))


async def async_setup_platform(  # NOSONAR python:S7503 - HA platform API
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Create the companion action-reason sensor for a YAML-discovered climate."""
    # HA passes both positionally but this platform only uses discovery_info.
    del hass
    del config
    if discovery_info is None:
        # This platform is only instantiated via discovery from climate.py.
        return

    name = discovery_info["name"]
    sensor_key = discovery_info["sensor_key"]

    async_add_entities(_sensors(sensor_key, name, discovery_info))
