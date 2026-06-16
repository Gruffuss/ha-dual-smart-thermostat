"""Unit tests for preset action (fan mode + switches) support."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.dual_smart_thermostat.managers.preset_action_manager import (
    PresetActionManager,
)
from custom_components.dual_smart_thermostat.preset_env.preset_env import PresetEnv


def test_preset_env_parses_fan_mode_and_switches():
    env = PresetEnv(
        temperature=18,
        fan_mode="quiet",
        switches=["switch.office_ac_sleep_2"],
    )
    assert env.fan_mode == "quiet"
    assert env.switches == ["switch.office_ac_sleep_2"]
    assert env.has_fan_mode() is True
    assert env.has_switches() is True


def test_preset_env_without_actions_defaults_none():
    env = PresetEnv(temperature=20)
    assert env.fan_mode is None
    assert env.switches is None
    assert env.has_fan_mode() is False
    assert env.has_switches() is False

    # Empty switch list is falsy: has_switches() must be False, not None-only.
    env_empty = PresetEnv(temperature=20, switches=[])
    assert env_empty.has_switches() is False


def _make_manager(fan_device=None, supports_fan_mode=False, states=None):
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    state_map = states or {}
    hass.states.get = lambda eid: state_map.get(eid)

    features = MagicMock()
    features.fan_device = fan_device
    features.supports_fan_mode = supports_fan_mode

    return hass, features, PresetActionManager(hass, features)


def _state(value):
    s = MagicMock()
    s.state = value
    return s


@pytest.mark.asyncio
async def test_apply_sets_fan_mode_and_turns_on_switches():
    fan_device = MagicMock()
    fan_device.fan_modes = ["auto", "low", "quiet"]
    fan_device.current_fan_mode = "low"
    fan_device.async_set_fan_mode = AsyncMock()

    hass, features, mgr = _make_manager(
        fan_device=fan_device,
        supports_fan_mode=True,
        states={"switch.sleep": _state("off")},
    )
    env = PresetEnv(temperature=18, fan_mode="quiet", switches=["switch.sleep"])

    await mgr.async_apply(env)

    fan_device.async_set_fan_mode.assert_awaited_once_with("quiet")
    hass.services.async_call.assert_awaited_once_with(
        "homeassistant", "turn_on", {"entity_id": "switch.sleep"}, blocking=True
    )
    # Baseline captured the prior state for later restore.
    assert mgr.serialize_baseline() == {
        "fan_mode": "low",
        "switches": {"switch.sleep": "off"},
    }


@pytest.mark.asyncio
async def test_apply_skips_unsupported_fan_mode_value():
    fan_device = MagicMock()
    fan_device.fan_modes = ["auto", "low"]
    fan_device.current_fan_mode = "low"
    fan_device.async_set_fan_mode = AsyncMock()

    _, _, mgr = _make_manager(fan_device=fan_device, supports_fan_mode=True)
    env = PresetEnv(temperature=18, fan_mode="quiet")  # "quiet" not in fan_modes

    await mgr.async_apply(env)

    fan_device.async_set_fan_mode.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_noop_when_no_actions():
    hass, _, mgr = _make_manager()
    await mgr.async_apply(PresetEnv(temperature=20))
    hass.services.async_call.assert_not_awaited()
    assert mgr.serialize_baseline() is None


@pytest.mark.asyncio
async def test_apply_skips_fan_mode_when_fan_modes_empty():
    fan_device = MagicMock()
    fan_device.fan_modes = []  # capability detection not ready
    fan_device.current_fan_mode = None
    fan_device.async_set_fan_mode = AsyncMock()

    _, _, mgr = _make_manager(fan_device=fan_device, supports_fan_mode=True)
    await mgr.async_apply(PresetEnv(temperature=18, fan_mode="quiet"))

    fan_device.async_set_fan_mode.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_skips_fan_mode_when_unsupported_by_device():
    fan_device = MagicMock()
    fan_device.fan_modes = ["auto", "low", "quiet"]
    fan_device.current_fan_mode = "low"
    fan_device.async_set_fan_mode = AsyncMock()

    # Device present but supports_fan_mode False -> must skip.
    _, _, mgr = _make_manager(fan_device=fan_device, supports_fan_mode=False)
    await mgr.async_apply(PresetEnv(temperature=18, fan_mode="quiet"))

    fan_device.async_set_fan_mode.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_skips_unavailable_switch():
    from homeassistant.const import STATE_UNAVAILABLE

    hass, _, mgr = _make_manager(states={"switch.sleep": _state(STATE_UNAVAILABLE)})
    await mgr.async_apply(PresetEnv(temperature=18, switches=["switch.sleep"]))

    hass.services.async_call.assert_not_awaited()
    # Unavailable switch is not captured into the baseline.
    assert mgr.serialize_baseline() == {"fan_mode": None, "switches": {}}


@pytest.mark.asyncio
async def test_second_apply_does_not_overwrite_baseline():
    hass, _, mgr = _make_manager(states={"switch.sleep": _state("off")})
    await mgr.async_apply(PresetEnv(temperature=18, switches=["switch.sleep"]))
    first = mgr.serialize_baseline()

    # Simulate the switch now being on (preset applied it), then apply again.
    hass.states.get = lambda eid: _state("on") if eid == "switch.sleep" else None
    await mgr.async_apply(PresetEnv(temperature=18, switches=["switch.sleep"]))

    # Baseline still reflects the ORIGINAL pre-preset state ("off").
    assert (
        mgr.serialize_baseline()
        == first
        == {
            "fan_mode": None,
            "switches": {"switch.sleep": "off"},
        }
    )
