"""Unit tests for preset action (fan mode + switches) support."""

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
