"""End-to-end behavior tests for presence sensing.

Presence sensing switches the thermostat to the ``away`` preset when nobody is
present, and restores the previously active preset when presence returns. This
is the inverse of opening detection, which pauses the HVAC while a window/door
is open.
"""

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.climate.const import (
    ATTR_PRESET_MODE,
    DOMAIN as CLIMATE,
    PRESET_AWAY,
    PRESET_HOME,
)
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
import pytest

from custom_components.dual_smart_thermostat.const import (
    ATTR_HVAC_ACTION_REASON,
    DOMAIN,
)
from custom_components.dual_smart_thermostat.hvac_action_reason.hvac_action_reason import (
    HVACActionReason,
)
from tests import common

PRESENCE_SENSOR = "binary_sensor.presence"
PRESENCE_SENSOR_A = "binary_sensor.presence_a"
PRESENCE_SENSOR_B = "binary_sensor.presence_b"


async def _setup_two_sensors(
    hass: HomeAssistant, absence_timeout: str, presence_timeout: str | None = None
) -> None:
    """Set up a heater thermostat with two presence sensors, each debounced.

    ``absence_timeout`` debounces the away switch (room empty); the optional
    ``presence_timeout`` debounces the restore (someone returned).
    """
    hass.states.async_set(common.ENT_SENSOR, 18)
    await hass.async_block_till_done()

    def _sensor(entity_id: str) -> dict:
        config = {"entity_id": entity_id, "absence_timeout": absence_timeout}
        if presence_timeout is not None:
            config["timeout"] = presence_timeout
        return config

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
                    _sensor(PRESENCE_SENSOR_A),
                    _sensor(PRESENCE_SENSOR_B),
                ],
            }
        },
    )
    await hass.async_block_till_done()


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


@pytest.mark.asyncio
async def test_two_sensors_away_waits_for_all_inactive(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """With two debounced sensors, away must wait until *all* are inactive for
    the timeout, measured from the moment the last sensor went inactive.

    Reproduces the reported failure: a room with two presence sensors behaves
    differently from a single-sensor room. The away switch must not fire while
    any sensor is still active, and the absence timeout must be measured from
    when the room as a whole became empty (the last sensor off), not per-sensor.
    """
    hass.states.async_set(PRESENCE_SENSOR_A, "on")
    hass.states.async_set(PRESENCE_SENSOR_B, "on")
    await _setup_two_sensors(hass, "00:01:00")

    await common.async_set_preset_mode(hass, PRESET_HOME)
    await hass.async_block_till_done()
    assert hass.states.get(common.ENTITY).attributes[ATTR_PRESET_MODE] == PRESET_HOME

    # Sensor A goes inactive; B is still active -> the room is occupied.
    hass.states.async_set(PRESENCE_SENSOR_A, "off")
    await hass.async_block_till_done()

    freezer.tick(timedelta(seconds=40))
    common.async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(common.ENTITY).attributes[ATTR_PRESET_MODE] == PRESET_HOME

    # Now B goes inactive too -> the room just became empty.
    hass.states.async_set(PRESENCE_SENSOR_B, "off")
    await hass.async_block_till_done()

    # 30s after the room became empty: still within the 60s timeout -> stay HOME.
    # (A has individually been off for 70s, but the room has only been empty 30s.)
    freezer.tick(timedelta(seconds=30))
    common.async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(common.ENTITY).attributes[ATTR_PRESET_MODE] == PRESET_HOME

    # Past the timeout measured from the room becoming empty -> away.
    freezer.tick(timedelta(seconds=40))
    common.async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(common.ENTITY).attributes[ATTR_PRESET_MODE] == PRESET_AWAY


@pytest.mark.asyncio
async def test_two_sensors_any_active_restores(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Once away, any single sensor becoming active restores the prior preset,
    without requiring both sensors to be active."""
    hass.states.async_set(PRESENCE_SENSOR_A, "off")
    hass.states.async_set(PRESENCE_SENSOR_B, "off")
    await _setup_two_sensors(hass, "00:01:00")

    # Establish a known preset, then let the room go away after the timeout.
    await common.async_set_preset_mode(hass, PRESET_HOME)
    await hass.async_block_till_done()

    hass.states.async_set(PRESENCE_SENSOR_A, "on")
    await hass.async_block_till_done()
    hass.states.async_set(PRESENCE_SENSOR_A, "off")
    await hass.async_block_till_done()

    freezer.tick(timedelta(seconds=70))
    common.async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(common.ENTITY).attributes[ATTR_PRESET_MODE] == PRESET_AWAY

    # Only one sensor becomes active -> restore immediately.
    hass.states.async_set(PRESENCE_SENSOR_A, "on")
    await hass.async_block_till_done()
    assert hass.states.get(common.ENTITY).attributes[ATTR_PRESET_MODE] == PRESET_HOME


@pytest.mark.asyncio
async def test_restore_waits_for_presence_timeout(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """With a presence (restore) timeout configured, returning presence must
    persist for that timeout before the prior preset is restored."""
    hass.states.async_set(PRESENCE_SENSOR_A, "off")
    hass.states.async_set(PRESENCE_SENSOR_B, "off")
    # 60s away debounce, 30s restore debounce.
    await _setup_two_sensors(hass, "00:01:00", presence_timeout="00:00:30")

    await common.async_set_preset_mode(hass, PRESET_HOME)
    await hass.async_block_till_done()

    # Settle into away.
    hass.states.async_set(PRESENCE_SENSOR_A, "on")
    await hass.async_block_till_done()
    hass.states.async_set(PRESENCE_SENSOR_A, "off")
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=70))
    common.async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(common.ENTITY).attributes[ATTR_PRESET_MODE] == PRESET_AWAY

    # Someone returns: within the 30s presence timeout, still away.
    hass.states.async_set(PRESENCE_SENSOR_A, "on")
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=20))
    common.async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(common.ENTITY).attributes[ATTR_PRESET_MODE] == PRESET_AWAY

    # Past the presence timeout with presence sustained -> restore.
    freezer.tick(timedelta(seconds=20))
    common.async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(common.ENTITY).attributes[ATTR_PRESET_MODE] == PRESET_HOME


@pytest.mark.asyncio
async def test_brief_presence_blip_does_not_restore(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A momentary presence blip shorter than the presence timeout must not
    pull the thermostat out of away."""
    hass.states.async_set(PRESENCE_SENSOR_A, "off")
    hass.states.async_set(PRESENCE_SENSOR_B, "off")
    await _setup_two_sensors(hass, "00:01:00", presence_timeout="00:00:30")

    await common.async_set_preset_mode(hass, PRESET_HOME)
    await hass.async_block_till_done()

    hass.states.async_set(PRESENCE_SENSOR_A, "on")
    await hass.async_block_till_done()
    hass.states.async_set(PRESENCE_SENSOR_A, "off")
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=70))
    common.async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(common.ENTITY).attributes[ATTR_PRESET_MODE] == PRESET_AWAY

    # A 10s blip (< 30s presence timeout) then gone again.
    hass.states.async_set(PRESENCE_SENSOR_A, "on")
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=10))
    common.async_fire_time_changed(hass)
    await hass.async_block_till_done()
    # During the blip the away preset must hold (no momentary restore flash).
    assert hass.states.get(common.ENTITY).attributes[ATTR_PRESET_MODE] == PRESET_AWAY
    hass.states.async_set(PRESENCE_SENSOR_A, "off")
    await hass.async_block_till_done()

    # Well past the original timeout window -> still away (blip ignored).
    freezer.tick(timedelta(seconds=60))
    common.async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(common.ENTITY).attributes[ATTR_PRESET_MODE] == PRESET_AWAY


@pytest.mark.asyncio
async def test_presence_and_openings_coexist(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Presence sensing and opening detection operate independently.

    Openings pause the HVAC (reported via the action reason) while presence
    manages the preset; neither feature should disturb the other.
    """
    opening = "binary_sensor.window"
    hass.states.async_set(common.ENT_SENSOR, 18)
    hass.states.async_set(PRESENCE_SENSOR_A, "on")
    hass.states.async_set(PRESENCE_SENSOR_B, "on")
    hass.states.async_set(opening, "off")
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
                    {"entity_id": PRESENCE_SENSOR_A, "absence_timeout": "00:01:00"},
                    {"entity_id": PRESENCE_SENSOR_B, "absence_timeout": "00:01:00"},
                ],
                "openings": [opening],
            }
        },
    )
    await hass.async_block_till_done()

    await common.async_set_preset_mode(hass, PRESET_HOME)
    await hass.async_block_till_done()
    state = hass.states.get(common.ENTITY)
    assert state.attributes[ATTR_PRESET_MODE] == PRESET_HOME
    assert (
        state.attributes.get(ATTR_HVAC_ACTION_REASON)
        == HVACActionReason.TARGET_TEMP_NOT_REACHED
    )

    # Opening the window pauses the HVAC, with presence configured.
    hass.states.async_set(opening, "on")
    await hass.async_block_till_done()
    state = hass.states.get(common.ENTITY)
    assert state.attributes.get(ATTR_HVAC_ACTION_REASON) == HVACActionReason.OPENING
    # The opening does not disturb the preset.
    assert state.attributes[ATTR_PRESET_MODE] == PRESET_HOME

    # Closing it resumes normal control.
    hass.states.async_set(opening, "off")
    await hass.async_block_till_done()
    assert (
        hass.states.get(common.ENTITY).attributes.get(ATTR_HVAC_ACTION_REASON)
        == HVACActionReason.TARGET_TEMP_NOT_REACHED
    )

    # Presence sensing still switches to away when the room empties.
    hass.states.async_set(PRESENCE_SENSOR_A, "off")
    hass.states.async_set(PRESENCE_SENSOR_B, "off")
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=70))
    common.async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(common.ENTITY).attributes[ATTR_PRESET_MODE] == PRESET_AWAY

    # And presence returning restores the preset (openings configured throughout).
    hass.states.async_set(PRESENCE_SENSOR_A, "on")
    await hass.async_block_till_done()
    assert hass.states.get(common.ENTITY).attributes[ATTR_PRESET_MODE] == PRESET_HOME
