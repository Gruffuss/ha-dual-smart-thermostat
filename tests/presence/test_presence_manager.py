"""Unit tests for the PresenceManager.

Presence sensing is the inverse of opening detection: an absence of presence
should make the thermostat consider switching to the ``away`` preset. These
tests cover the state-tracking surface (detection, scope and the safe
defaults) independently of the climate entity wiring.
"""

import pytest

from custom_components.dual_smart_thermostat.const import (
    CONF_PRESENCE,
    CONF_PRESENCE_SCOPE,
)
from custom_components.dual_smart_thermostat.managers.presence_manager import (
    PresenceHvacModeScope,
    PresenceManager,
)


@pytest.mark.asyncio
async def test_no_presence_sensors_defaults_to_present(hass):
    """With no sensors configured, presence is always considered detected."""
    manager = PresenceManager(hass, {})
    assert manager.presence_entities == []
    assert manager.is_presence_detected() is True


@pytest.mark.asyncio
async def test_single_sensor_present_and_absent(hass):
    """A single sensor reflects present (on) and absent (off)."""
    config = {CONF_PRESENCE: [{"entity_id": "binary_sensor.presence"}]}
    manager = PresenceManager(hass, config)

    hass.states.async_set("binary_sensor.presence", "on")
    await hass.async_block_till_done()
    assert manager.is_presence_detected() is True

    hass.states.async_set("binary_sensor.presence", "off")
    await hass.async_block_till_done()
    assert manager.is_presence_detected() is False


@pytest.mark.asyncio
async def test_any_sensor_present_means_present(hass):
    """If any configured sensor reports present, presence is detected."""
    config = {
        CONF_PRESENCE: [
            {"entity_id": "binary_sensor.presence_1"},
            {"entity_id": "binary_sensor.presence_2"},
        ]
    }
    manager = PresenceManager(hass, config)

    hass.states.async_set("binary_sensor.presence_1", "off")
    hass.states.async_set("binary_sensor.presence_2", "on")
    await hass.async_block_till_done()
    assert manager.is_presence_detected() is True

    hass.states.async_set("binary_sensor.presence_2", "off")
    await hass.async_block_till_done()
    assert manager.is_presence_detected() is False


@pytest.mark.asyncio
async def test_unavailable_sensor_is_safe_present(hass):
    """A sensor outage must never force the away switch."""
    config = {CONF_PRESENCE: [{"entity_id": "binary_sensor.presence"}]}
    manager = PresenceManager(hass, config)

    hass.states.async_set("binary_sensor.presence", "unavailable")
    await hass.async_block_till_done()
    assert manager.is_presence_detected() is True

    # Sensor with no state at all is also treated as present.
    manager_missing = PresenceManager(
        hass, {CONF_PRESENCE: [{"entity_id": "binary_sensor.missing"}]}
    )
    assert manager_missing.is_presence_detected() is True


@pytest.mark.asyncio
async def test_home_state_counts_as_present(hass):
    """device_tracker-style ``home`` is treated as present too."""
    config = {CONF_PRESENCE: [{"entity_id": "binary_sensor.presence"}]}
    manager = PresenceManager(hass, config)

    hass.states.async_set("binary_sensor.presence", "home")
    await hass.async_block_till_done()
    assert manager.is_presence_detected() is True


@pytest.mark.asyncio
async def test_scope_limits_modes(hass):
    """Out-of-scope HVAC modes report present so they are never forced away."""
    config = {
        CONF_PRESENCE: [{"entity_id": "binary_sensor.presence"}],
        CONF_PRESENCE_SCOPE: [PresenceHvacModeScope.COOL],
    }
    manager = PresenceManager(hass, config)

    hass.states.async_set("binary_sensor.presence", "off")
    await hass.async_block_till_done()

    # In scope (cool) -> absence honored.
    assert manager.is_presence_detected(PresenceHvacModeScope.COOL) is False
    # Out of scope (heat) -> always present.
    assert manager.is_presence_detected(PresenceHvacModeScope.HEAT) is True


@pytest.mark.asyncio
async def test_conform_helpers(hass):
    """Plain entity ids and dicts are both normalized to the dict form."""
    manager = PresenceManager(
        hass,
        {
            CONF_PRESENCE: [
                "binary_sensor.a",
                {"entity_id": "binary_sensor.b", "absence_timeout": 30},
            ]
        },
    )
    assert manager.presence_entities == ["binary_sensor.a", "binary_sensor.b"]
    assert manager.presence[0] == {"entity_id": "binary_sensor.a"}
