import logging
import re
from typing import Any

from homeassistant.components.climate.const import (
    ATTR_HUMIDITY,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
)
from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.template import Template

from ..const import (
    CONF_FAN_MODE,
    CONF_MAX_FLOOR_TEMP,
    CONF_MIN_FLOOR_TEMP,
    CONF_PRESET_SWITCHES,
)

_LOGGER = logging.getLogger(__name__)

# Config key -> PresetEnv attribute for the fields preset auto-save may
# overwrite with the values a user dialed in from the UI. Kept in one place so
# the manager, the persistence layer and the config flow agree on the names.
AUTO_SAVE_FIELDS: dict[str, str] = {
    ATTR_TEMPERATURE: "temperature",
    ATTR_TARGET_TEMP_LOW: "target_temp_low",
    ATTR_TARGET_TEMP_HIGH: "target_temp_high",
    ATTR_HUMIDITY: "humidity",
    CONF_FAN_MODE: "fan_mode",
}

_TEMP_FIELDS = ("temperature", "target_temp_low", "target_temp_high")


def normalize_preset_temperature(value: float | None, field_name: str) -> float | None:
    """Return preset temperature unless it is a non-physical placeholder.

    Some real-world configs use ``temperature: 0`` (or negative values) as a
    placeholder meaning "unset" inside a preset definition. Applying those
    would overwrite a valid target with a bogus value, so we treat anything
    at or below zero as unset and ignore it. Templates can also render such
    values at apply time, which is why this runs on every read rather than
    once at parse time.

    Note: we deliberately do NOT clamp against ``min_temp``/``max_temp``.
    Legitimate low presets such as ``PRESET_ANTI_FREEZE`` (5°C) sit below the
    default ``min_temp`` of 7°C and must still be honored.
    """
    if value is None:
        return None

    temp = float(value)

    if temp <= 0:
        _LOGGER.warning(
            "Ignoring preset %s=%s because it is a placeholder (<= 0)",
            field_name,
            temp,
        )
        return None

    return temp


class TargeTempEnv:
    temperature: float | None

    def __init__(self, **kwargs) -> None:
        super(TargeTempEnv, self).__init__(**kwargs)
        self.temperature = kwargs.get(ATTR_TEMPERATURE) or None


class RangeTempEnv:
    target_temp_low: float | None
    target_temp_high: float | None

    def __init__(self, **kwargs) -> None:
        super(RangeTempEnv, self).__init__(**kwargs)
        self.target_temp_low = kwargs.get(ATTR_TARGET_TEMP_LOW) or None
        self.target_temp_high = kwargs.get(ATTR_TARGET_TEMP_HIGH) or None


class FloorTempLimitEnv:
    min_floor_temp: float | None
    max_floor_temp: float | None

    def __init__(self, **kwargs) -> None:
        super(FloorTempLimitEnv, self).__init__(**kwargs)
        _LOGGER.debug(f"FloorTempLimitEnv kwargs: {kwargs}")
        self.min_floor_temp = kwargs.get(CONF_MIN_FLOOR_TEMP) or None
        self.max_floor_temp = kwargs.get(CONF_MAX_FLOOR_TEMP) or None


class TempEnv(TargeTempEnv, RangeTempEnv, FloorTempLimitEnv):
    def __init__(self, **kwargs) -> None:
        super(TempEnv, self).__init__(**kwargs)
        _LOGGER.debug(f"TempEnv kwargs: {kwargs}")


class HumidityEnv:
    humidity: float | None

    def __init__(self, **kwargs) -> None:
        super(HumidityEnv, self).__init__()
        _LOGGER.debug(f"HumidityEnv kwargs: {kwargs}")
        self.humidity = kwargs.get(ATTR_HUMIDITY) or None


class PresetEnv(TempEnv, HumidityEnv):
    def __init__(self, **kwargs):
        # Initialize template tracking structures BEFORE calling super().__init__()
        self._template_fields: dict[str, str] = {}  # field_name -> template_string
        self._last_good_values: dict[str, float] = (
            {}
        )  # field_name -> last successful value
        self._referenced_entities: set[str] = (
            set()
        )  # entity_ids referenced in templates

        super(PresetEnv, self).__init__(**kwargs)
        _LOGGER.debug(f"kwargs: {kwargs}")

        # Process temperature fields for template detection
        self._process_field("temperature", kwargs.get(ATTR_TEMPERATURE))
        self._process_field("target_temp_low", kwargs.get(ATTR_TARGET_TEMP_LOW))
        self._process_field("target_temp_high", kwargs.get(ATTR_TARGET_TEMP_HIGH))

        # Action fields: applied to the wrapped AC when the preset activates.
        self.fan_mode = kwargs.get(CONF_FAN_MODE) or None
        self.switches = kwargs.get(CONF_PRESET_SWITCHES) or None

    def _process_field(self, field_name: str, value: Any) -> None:
        """Process temperature field to determine if static or template."""
        if value is None:
            return

        if isinstance(value, (int, float)):
            # Static value - store as float and set last_good_value
            setattr(self, field_name, float(value))
            self._last_good_values[field_name] = float(value)
            _LOGGER.debug(
                f"PresetEnv: {field_name} stored as static value: {float(value)}"
            )
        elif isinstance(value, str):
            # Try to parse as number first (config stores numbers as strings)
            try:
                float_val = float(value)
                # It's a numeric string, treat as static value
                setattr(self, field_name, float_val)
                self._last_good_values[field_name] = float_val
                _LOGGER.debug(
                    f"PresetEnv: {field_name} stored as static value from string: {float_val}"
                )
                return
            except ValueError:
                pass  # Not a number, treat as template

            # Template string - store in template_fields and extract entities
            self._template_fields[field_name] = value
            self._extract_entities(value)
            _LOGGER.debug(f"PresetEnv: {field_name} detected as template: {value}")

    def _extract_entities(self, template_str: str) -> None:
        """Extract entity IDs from template string using regex.

        Parses template strings for entity_id patterns like:
        - states('sensor.temperature')
        - is_state('binary_sensor.motion', 'on')
        - state_attr('climate.thermostat', 'temperature')
        """
        try:
            # Pattern to match entity IDs in common template functions
            # Matches: states('entity.id'), is_state('entity.id', ...), state_attr('entity.id', ...)
            pattern = (
                r"(?:states|is_state|state_attr)\s*\(\s*['\"]([a-z_]+\.[a-z0-9_]+)['\"]"
            )
            matches = re.findall(pattern, template_str, re.IGNORECASE)

            if matches:
                self._referenced_entities.update(matches)
                _LOGGER.debug(f"PresetEnv: Extracted entities from template: {matches}")
        except Exception as e:
            _LOGGER.debug(f"PresetEnv: Could not extract entities from template: {e}")

    def get_temperature(self, hass: HomeAssistant) -> float | None:
        """Get temperature, evaluating template if needed.

        Placeholder values (<= 0) are filtered to None here so every consumer
        — apply, restore, and template re-evaluation — gets the same guard.
        """
        if "temperature" in self._template_fields:
            value = self._evaluate_template(hass, "temperature")
        else:
            value = self.temperature
        return normalize_preset_temperature(value, "temperature")

    def get_target_temp_low(self, hass: HomeAssistant) -> float | None:
        """Get target_temp_low, evaluating template if needed."""
        if "target_temp_low" in self._template_fields:
            value = self._evaluate_template(hass, "target_temp_low")
        else:
            value = self.target_temp_low
        return normalize_preset_temperature(value, "target_temp_low")

    def get_target_temp_high(self, hass: HomeAssistant) -> float | None:
        """Get target_temp_high, evaluating template if needed."""
        if "target_temp_high" in self._template_fields:
            value = self._evaluate_template(hass, "target_temp_high")
        else:
            value = self.target_temp_high
        return normalize_preset_temperature(value, "target_temp_high")

    def _evaluate_template(self, hass: HomeAssistant, field_name: str) -> float:
        """Safely evaluate template with fallback to previous value."""
        template_str = self._template_fields.get(field_name)
        if not template_str:
            # No template for this field, return last good value or default
            return self._last_good_values.get(field_name, 20.0)

        try:
            template = Template(template_str, hass)
            # Note: async_render is actually synchronous despite the name
            result = template.async_render()
            temp = float(result)

            # Update last good value
            self._last_good_values[field_name] = temp
            _LOGGER.debug(
                f"PresetEnv: Template evaluation success for {field_name}: {template_str} -> {temp}"
            )
            return temp
        except Exception as e:
            # Keep previous value on error
            previous = self._last_good_values.get(field_name, 20.0)
            _LOGGER.warning(
                f"PresetEnv: Template evaluation failed for {field_name}. "
                f"Template: {template_str}, Entities: {self._referenced_entities}, "
                f"Error: {e}, Keeping previous: {previous}"
            )
            return previous

    @property
    def referenced_entities(self) -> set[str]:
        """Return set of entities referenced in templates."""
        return self._referenced_entities

    def has_templates(self) -> bool:
        """Check if this preset uses any templates."""
        return len(self._template_fields) > 0

    @property
    def to_dict(self) -> dict:
        return self.__dict__

    def is_template_field(self, field_name: str) -> bool:
        """Return True when the field is defined by a template, not a number."""
        return field_name in self._template_fields

    def update_values(self, values: dict[str, Any]) -> dict[str, Any]:
        """Overwrite preset fields with live values (preset auto-save).

        ``values`` is keyed by preset *config* key (``temperature``,
        ``target_temp_low``, ``target_temp_high``, ``humidity``,
        ``fan_mode``). Returns the subset that was actually applied so callers
        know whether anything needs persisting.

        Template-backed fields are skipped: the template *is* the preset's
        definition, so a one-off nudge from the UI must not silently replace it
        with a frozen number. Non-physical temperatures (<= 0) are skipped for
        the same reason ``normalize_preset_temperature`` ignores them on read.
        """
        applied: dict[str, Any] = {}

        for key, value in values.items():
            field_name = AUTO_SAVE_FIELDS.get(key)
            if field_name is None or value is None:
                continue

            if self.is_template_field(field_name):
                _LOGGER.debug(
                    "Preset auto-save skipping %s: defined by template %s",
                    field_name,
                    self._template_fields[field_name],
                )
                continue

            if field_name in _TEMP_FIELDS:
                value = normalize_preset_temperature(value, field_name)
                if value is None:
                    continue

            if getattr(self, field_name) == value:
                continue

            setattr(self, field_name, value)
            if field_name in _TEMP_FIELDS:
                self._last_good_values[field_name] = float(value)
            applied[key] = value

        if applied:
            _LOGGER.debug("Preset auto-save applied values: %s", applied)

        return applied

    def to_config_dict(self) -> dict[str, Any]:
        """Return the preset as a config-shaped dict for persistence.

        Mirrors the nested format the config/options flows store
        (``{"away": {"temperature": 17.0}}``) and keeps template fields as
        their original template strings.
        """
        config: dict[str, Any] = {}

        for key, field_name in AUTO_SAVE_FIELDS.items():
            if field_name in self._template_fields:
                config[key] = self._template_fields[field_name]
                continue
            value = getattr(self, field_name)
            if value is not None:
                config[key] = value

        if self.min_floor_temp is not None:
            config[CONF_MIN_FLOOR_TEMP] = self.min_floor_temp
        if self.max_floor_temp is not None:
            config[CONF_MAX_FLOOR_TEMP] = self.max_floor_temp
        if self.switches:
            config[CONF_PRESET_SWITCHES] = list(self.switches)

        return config

    def has_temp_range(self) -> bool:
        return self.target_temp_low is not None and self.target_temp_high is not None

    def has_temp(self) -> bool:
        return self.temperature is not None

    def has_humidity(self) -> bool:
        return self.humidity is not None

    def has_fan_mode(self) -> bool:
        return self.fan_mode is not None

    def has_switches(self) -> bool:
        return bool(self.switches)

    def has_floor_temp_limits(self) -> bool:
        return self.min_floor_temp is not None or self.max_floor_temp is not None
