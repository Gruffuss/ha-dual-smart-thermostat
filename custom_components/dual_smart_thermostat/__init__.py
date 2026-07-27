"""The dual_smart_thermostat component."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DATA_PRESET_AUTO_SAVE_WRITES

_LOGGER = logging.getLogger(__name__)

DOMAIN = "dual_smart_thermostat"
PLATFORMS = [Platform.CLIMATE, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up from a config entry."""
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(config_entry_update_listener))
    return True


async def config_entry_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update listener, called when the config entry options are changed."""
    if _consume_preset_auto_save_write(hass, entry):
        # Preset auto-save wrote a value the running entity already holds;
        # reloading here would tear it down for nothing.
        _LOGGER.debug(
            "Skipping reload for preset auto-save write on entry %s", entry.entry_id
        )
        return

    await hass.config_entries.async_reload(entry.entry_id)


def _consume_preset_auto_save_write(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Return True if this update came from a preset auto-save write."""
    pending = hass.data.get(DOMAIN, {}).get(DATA_PRESET_AUTO_SAVE_WRITES, {})
    if not pending.get(entry.entry_id):
        return False

    pending[entry.entry_id] -= 1
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    hass.data.get(DOMAIN, {}).get(DATA_PRESET_AUTO_SAVE_WRITES, {}).pop(
        entry.entry_id, None
    )
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
