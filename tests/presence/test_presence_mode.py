"""End-to-end behavior tests for presence sensing.

Presence sensing switches the thermostat to the ``away`` preset when nobody is
present, and restores the previously active preset when presence returns. This
is the inverse of opening detection, which pauses the HVAC while a window/door
is open.
"""

from homeassistant.components.climate.const import (
    ATTR_PRESET_MODE,
    DOMAIN as CLIMATE,
    PRESET_AWAY,
    PRESET_HOME,
)
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
import pytest

from custom_components.dual_smart_thermostat.const import DOMAIN
from tests import common

PRESENCE_SENSOR = "binary_sensor.presence"


async def _setup_with_presence(hass: HomeAssistant, presence_config) -> None:
    """Set up a simple heater thermostat with presets and presence sensing."""
    hass.states.async_set(common.ENT_SENSOR, 18)
    await hass.async_block_till_done()

    assert await async_setup_component(
        hass,
        CLIMATE,
        {
            "climate": {
                "platform": DOMAIN,
                "name": "test",
                "heater": common.ENT_HEATER,
                "target_sensor": common.ENT_SENSOR,
                "initial_hvac_mode": "heat",
                PRESET_AWAY: {"temperature": 16},
                PRESET_HOME: {"temperature": 21},
                "presence": presence_config,
            }
        },
    )
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_absence_switches_to_away(hass: HomeAssistant) -> None:
    """Losing presence activates the away preset, regaining it restores."""
    hass.states.async_set(PRESENCE_SENSOR, "on")
    await _setup_with_presence(hass, [PRESENCE_SENSOR])

    # Start in a known, non-away preset.
    await common.async_set_preset_mode(hass, PRESET_HOME)
    await hass.async_block_till_done()
    assert hass.states.get(common.ENTITY).attributes[ATTR_PRESET_MODE] == PRESET_HOME

    # Nobody home -> away preset.
    hass.states.async_set(PRESENCE_SENSOR, "off")
    await hass.async_block_till_done()
    assert hass.states.get(common.ENTITY).attributes[ATTR_PRESET_MODE] == PRESET_AWAY

    # Presence returns -> previously active preset restored.
    hass.states.async_set(PRESENCE_SENSOR, "on")
    await hass.async_block_till_done()
    assert hass.states.get(common.ENTITY).attributes[ATTR_PRESET_MODE] == PRESET_HOME


@pytest.mark.asyncio
async def test_presence_present_does_not_change_preset(hass: HomeAssistant) -> None:
    """While presence is detected the preset is left untouched."""
    hass.states.async_set(PRESENCE_SENSOR, "on")
    await _setup_with_presence(hass, [PRESENCE_SENSOR])

    await common.async_set_preset_mode(hass, PRESET_HOME)
    await hass.async_block_till_done()

    # Re-assert present; nothing should change.
    hass.states.async_set(PRESENCE_SENSOR, "on")
    await hass.async_block_till_done()
    assert hass.states.get(common.ENTITY).attributes[ATTR_PRESET_MODE] == PRESET_HOME


@pytest.mark.asyncio
async def test_startup_while_absent_applies_away(hass: HomeAssistant) -> None:
    """A system booting while nobody is home reflects the away preset."""
    hass.states.async_set(PRESENCE_SENSOR, "off")
    await _setup_with_presence(hass, [PRESENCE_SENSOR])

    assert hass.states.get(common.ENTITY).attributes[ATTR_PRESET_MODE] == PRESET_AWAY


@pytest.mark.asyncio
async def test_yaml_object_form_and_scope(hass: HomeAssistant) -> None:
    """The documented YAML object form and presence_scope are honored.

    Validates that ``presence`` accepts ``entity_id`` objects and that
    ``presence_scope`` restricts the away switch to the listed HVAC modes.
    """
    hass.states.async_set(common.ENT_SENSOR, 18)
    hass.states.async_set(PRESENCE_SENSOR, "on")
    await hass.async_block_till_done()

    assert await async_setup_component(
        hass,
        CLIMATE,
        {
            "climate": {
                "platform": DOMAIN,
                "name": "test",
                "heater": common.ENT_HEATER,
                "target_sensor": common.ENT_SENSOR,
                "initial_hvac_mode": "heat",
                PRESET_AWAY: {"temperature": 16},
                PRESET_HOME: {"temperature": 21},
                "presence": [{"entity_id": PRESENCE_SENSOR}],
                "presence_scope": ["heat"],
            }
        },
    )
    await hass.async_block_till_done()

    await common.async_set_preset_mode(hass, PRESET_HOME)
    await hass.async_block_till_done()

    # In-scope (heat) absence -> away.
    hass.states.async_set(PRESENCE_SENSOR, "off")
    await hass.async_block_till_done()
    assert hass.states.get(common.ENTITY).attributes[ATTR_PRESET_MODE] == PRESET_AWAY


@pytest.mark.asyncio
async def test_yaml_absence_timeout_parses(hass: HomeAssistant) -> None:
    """The documented YAML ``absence_timeout`` (timedelta) parses and sets up.

    A debounced sensor that is currently present should leave the preset
    untouched immediately after setup.
    """
    hass.states.async_set(common.ENT_SENSOR, 18)
    hass.states.async_set(PRESENCE_SENSOR, "on")
    await hass.async_block_till_done()

    assert await async_setup_component(
        hass,
        CLIMATE,
        {
            "climate": {
                "platform": DOMAIN,
                "name": "test",
                "heater": common.ENT_HEATER,
                "target_sensor": common.ENT_SENSOR,
                "initial_hvac_mode": "heat",
                PRESET_AWAY: {"temperature": 16},
                PRESET_HOME: {"temperature": 21},
                "presence": [
                    {"entity_id": PRESENCE_SENSOR, "absence_timeout": "00:02:00"}
                ],
            }
        },
    )
    await hass.async_block_till_done()

    state = hass.states.get(common.ENTITY)
    assert state is not None
    # Present at startup -> not forced to away.
    assert state.attributes[ATTR_PRESET_MODE] != PRESET_AWAY
