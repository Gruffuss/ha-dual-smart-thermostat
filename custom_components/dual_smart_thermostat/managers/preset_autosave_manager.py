"""Writes UI adjustments back into the preset that is currently active.

Without this, temperature/humidity/fan changes made while a preset is selected
live only until the preset is re-applied (re-selecting it, presence restoring
it, a template re-render, or a Home Assistant restart), at which point the
configured value wins and the adjustment appears to revert. With auto-save on,
the adjustment *becomes* the preset's value and is persisted to the config
entry, so the preset config shown in the options flow stays in sync too.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate.const import (
    ATTR_HUMIDITY,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    PRESET_NONE,
)
from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from ..const import (
    CONF_FAN_MODE,
    CONF_PRESET_AUTO_SAVE,
    DATA_PRESET_AUTO_SAVE_WRITES,
    DEFAULT_PRESET_AUTO_SAVE,
    DOMAIN,
)
from ..managers.environment_manager import EnvironmentManager
from ..managers.feature_manager import FeatureManager
from ..managers.preset_manager import PresetManager

_LOGGER = logging.getLogger(__name__)


class PresetAutoSaveManager:
    """Persist live target changes into the active preset."""

    def __init__(
        self,
        hass: HomeAssistant,
        config: ConfigType,
        presets: PresetManager,
        environment: EnvironmentManager,
        features: FeatureManager,
        entry_id: str | None = None,
    ) -> None:
        self.hass = hass
        self._presets = presets
        self._environment = environment
        self._features = features
        # None for YAML setups: there is no config entry to write to, so
        # auto-save stays in-memory for the lifetime of the entity.
        self._entry_id = entry_id
        self._enabled = bool(
            config.get(CONF_PRESET_AUTO_SAVE, DEFAULT_PRESET_AUTO_SAVE)
        )

    @property
    def enabled(self) -> bool:
        """Return True when adjustments should be saved into the preset."""
        return self._enabled

    @property
    def is_active(self) -> bool:
        """Return True when a preset is selected that auto-save can write to."""
        if not self._enabled:
            return False
        preset_mode = self._presets.preset_mode
        return preset_mode not in (None, PRESET_NONE) and preset_mode in (
            self._presets.presets or {}
        )

    async def async_save_temperatures(self) -> bool:
        """Save the current target temperature(s) into the active preset."""
        if self._features.is_range_mode:
            values = {
                ATTR_TARGET_TEMP_LOW: self._environment.target_temp_low,
                ATTR_TARGET_TEMP_HIGH: self._environment.target_temp_high,
            }
        else:
            values = {ATTR_TEMPERATURE: self._environment.target_temp}

        return await self.async_save(values)

    async def async_save_humidity(self) -> bool:
        """Save the current target humidity into the active preset."""
        return await self.async_save({ATTR_HUMIDITY: self._environment.target_humidity})

    async def async_save_fan_mode(self, fan_mode: str | None) -> bool:
        """Save the selected fan mode into the active preset."""
        return await self.async_save({CONF_FAN_MODE: fan_mode})

    async def async_save(self, values: dict[str, Any]) -> bool:
        """Apply ``values`` to the active preset and persist it.

        Returns True when the preset was changed.
        """
        if not self.is_active:
            return False

        preset_mode = self._presets.preset_mode
        preset_env = self._presets.presets[preset_mode]

        applied = preset_env.update_values(values)
        if not applied:
            return False

        _LOGGER.info(
            "Preset auto-save: stored %s into preset '%s'", applied, preset_mode
        )

        self._persist_to_entry(preset_mode, preset_env.to_config_dict())
        return True

    def _persist_to_entry(self, preset_key: str, preset_config: dict[str, Any]) -> None:
        """Write the preset back to the config entry, without reloading it.

        The entry's update listener would normally reload the integration, which
        would tear the entity down mid-service-call for what is only a value
        refresh we already applied in memory. The pending-write counter tells
        the listener to skip exactly the reloads caused by our own writes.
        """
        if self._entry_id is None:
            _LOGGER.debug(
                "Preset auto-save: no config entry (YAML setup), keeping "
                "preset '%s' in memory only",
                preset_key,
            )
            return

        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            _LOGGER.debug("Preset auto-save: config entry %s gone", self._entry_id)
            return

        data = dict(entry.data)
        options = dict(entry.options)

        data[preset_key] = preset_config
        # climate.py merges data and options with options winning, so a stale
        # copy in options would mask the write.
        if preset_key in options:
            options[preset_key] = preset_config

        if data == dict(entry.data) and options == dict(entry.options):
            return

        self._mark_pending_write()
        try:
            changed = self.hass.config_entries.async_update_entry(
                entry, data=data, options=options
            )
        except Exception as ex:  # pragma: no cover - defensive
            self._clear_pending_write()
            _LOGGER.warning("Preset auto-save could not update config entry: %s", ex)
            return

        if not changed:
            # No listener will fire, so the pending write must not linger.
            self._clear_pending_write()

    def _pending_writes(self) -> dict[str, int]:
        return self.hass.data.setdefault(DOMAIN, {}).setdefault(
            DATA_PRESET_AUTO_SAVE_WRITES, {}
        )

    def _mark_pending_write(self) -> None:
        pending = self._pending_writes()
        pending[self._entry_id] = pending.get(self._entry_id, 0) + 1

    def _clear_pending_write(self) -> None:
        pending = self._pending_writes()
        if pending.get(self._entry_id):
            pending[self._entry_id] -= 1
