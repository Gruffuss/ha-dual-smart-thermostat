"""Tests for preset auto-save.

When ``preset_auto_save`` is enabled (the default), adjusting the target
temperature, humidity or fan speed from the thermostat UI while a preset is
active writes the new value back into that preset. This stops the adjustment
from being reverted the next time the preset is re-applied (re-selecting it,
presence restoring it, a template re-render, or a Home Assistant restart), and
keeps the preset config shown in the options flow in sync.
"""

from unittest.mock import MagicMock

from homeassistant.components.climate import (
    ATTR_PRESET_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_HOME,
    PRESET_NONE,
    SERVICE_SET_PRESET_MODE,
    SERVICE_SET_TEMPERATURE,
)
from homeassistant.const import ATTR_TEMPERATURE, STATE_OFF
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dual_smart_thermostat.const import (
    CONF_PRESET_AUTO_SAVE,
    DATA_PRESET_AUTO_SAVE_WRITES,
    DOMAIN,
)
from custom_components.dual_smart_thermostat.managers.preset_autosave_manager import (
    PresetAutoSaveManager,
)
from custom_components.dual_smart_thermostat.preset_env.preset_env import PresetEnv

# ---------------------------------------------------------------------------
# PresetEnv.update_values / to_config_dict (pure unit tests, no hass needed)
# ---------------------------------------------------------------------------


def test_update_values_overwrites_static_temperature():
    env = PresetEnv(temperature=17)

    applied = env.update_values({ATTR_TEMPERATURE: 21.5})

    assert applied == {ATTR_TEMPERATURE: 21.5}
    assert env.temperature == 21.5
    # get_temperature must also reflect the new value (last_good_values updated).
    assert env.get_temperature(MagicMock()) == 21.5


def test_update_values_noop_when_unchanged():
    env = PresetEnv(temperature=20)

    assert env.update_values({ATTR_TEMPERATURE: 20.0}) == {}


def test_update_values_skips_template_field():
    env = PresetEnv(temperature="{{ states('input_number.eco_temp') }}")
    assert env.is_template_field("temperature") is True

    # A one-off UI nudge must not replace the template with a frozen number.
    assert env.update_values({ATTR_TEMPERATURE: 21.5}) == {}
    assert env.to_config_dict()["temperature"] == (
        "{{ states('input_number.eco_temp') }}"
    )


def test_update_values_skips_placeholder_temperature():
    env = PresetEnv(temperature=18)

    # <= 0 is a placeholder meaning "unset"; auto-save must not store it.
    assert env.update_values({ATTR_TEMPERATURE: 0}) == {}
    assert env.temperature == 18.0


def test_update_values_range_and_humidity():
    env = PresetEnv(target_temp_low=16, target_temp_high=20, humidity=45)

    applied = env.update_values(
        {
            ATTR_TARGET_TEMP_LOW: 18.0,
            ATTR_TARGET_TEMP_HIGH: 22.0,
            "humidity": 50.0,
        }
    )

    assert applied == {
        ATTR_TARGET_TEMP_LOW: 18.0,
        ATTR_TARGET_TEMP_HIGH: 22.0,
        "humidity": 50.0,
    }
    config = env.to_config_dict()
    assert config[ATTR_TARGET_TEMP_LOW] == 18.0
    assert config[ATTR_TARGET_TEMP_HIGH] == 22.0
    assert config["humidity"] == 50.0


def test_to_config_dict_preserves_actions_and_floor_limits():
    env = PresetEnv(
        temperature=19,
        min_floor_temp=5,
        max_floor_temp=28,
        fan_mode="quiet",
        switches=["switch.ac_eco"],
    )

    config = env.to_config_dict()

    assert config["temperature"] == 19.0
    assert config["min_floor_temp"] == 5.0
    assert config["max_floor_temp"] == 28.0
    assert config["fan_mode"] == "quiet"
    assert config["switches"] == ["switch.ac_eco"]


# ---------------------------------------------------------------------------
# PresetAutoSaveManager (mocked hass / managers)
# ---------------------------------------------------------------------------


def _make_manager(
    *,
    enabled=True,
    preset_mode=PRESET_ECO,
    presets=None,
    is_range_mode=False,
    entry=None,
    entry_id="entry-1",
    hass=None,
):
    hass = hass or MagicMock()
    hass.data = {}
    hass.config_entries.async_get_entry = MagicMock(return_value=entry)
    hass.config_entries.async_update_entry = MagicMock(return_value=True)

    presets_manager = MagicMock()
    presets_manager.preset_mode = preset_mode
    presets_manager.presets = presets or {PRESET_ECO: PresetEnv(temperature=18)}

    environment = MagicMock()
    environment.target_temp = 21.0
    environment.target_temp_low = 18.0
    environment.target_temp_high = 22.0
    environment.target_humidity = 50.0

    features = MagicMock()
    features.is_range_mode = is_range_mode

    config = {CONF_PRESET_AUTO_SAVE: enabled}
    manager = PresetAutoSaveManager(
        hass, config, presets_manager, environment, features, entry_id=entry_id
    )
    return manager, hass, presets_manager, environment


def test_is_active_requires_enabled_and_real_preset():
    manager, *_ = _make_manager(enabled=True, preset_mode=PRESET_ECO)
    assert manager.is_active is True

    manager, *_ = _make_manager(enabled=False, preset_mode=PRESET_ECO)
    assert manager.is_active is False

    manager, *_ = _make_manager(enabled=True, preset_mode=PRESET_NONE)
    assert manager.is_active is False

    # Preset selected but not present in the configured presets map.
    manager, *_ = _make_manager(
        enabled=True, preset_mode=PRESET_HOME, presets={PRESET_ECO: PresetEnv()}
    )
    assert manager.is_active is False


async def test_save_temperatures_updates_preset_and_persists():
    entry = MagicMock()
    entry.data = {"eco": {"temperature": 18.0}}
    entry.options = {}
    manager, hass, presets_manager, _ = _make_manager(entry=entry)

    changed = await manager.async_save_temperatures()

    assert changed is True
    # Preset in memory now holds the live target.
    assert presets_manager.presets[PRESET_ECO].temperature == 21.0
    # Config entry was written with the new nested preset config...
    hass.config_entries.async_update_entry.assert_called_once()
    _, kwargs = hass.config_entries.async_update_entry.call_args
    assert kwargs["data"]["eco"]["temperature"] == 21.0
    # ...and a pending write was recorded so the reload listener skips it.
    assert hass.data[DOMAIN][DATA_PRESET_AUTO_SAVE_WRITES]["entry-1"] == 1


async def test_save_noop_when_value_unchanged_does_not_persist():
    entry = MagicMock()
    entry.data = {"eco": {"temperature": 21.0}}
    entry.options = {}
    manager, hass, _, environment = _make_manager(entry=entry)
    environment.target_temp = 18.0  # equals the preset's stored value

    changed = await manager.async_save_temperatures()

    assert changed is False
    hass.config_entries.async_update_entry.assert_not_called()


async def test_save_disabled_is_noop():
    manager, hass, presets_manager, _ = _make_manager(enabled=False)

    assert await manager.async_save_temperatures() is False
    hass.config_entries.async_update_entry.assert_not_called()


async def test_save_yaml_setup_stays_in_memory():
    # entry_id None => YAML setup, nothing to persist to.
    manager, hass, presets_manager, _ = _make_manager(entry_id=None)

    changed = await manager.async_save_temperatures()

    assert changed is True
    assert presets_manager.presets[PRESET_ECO].temperature == 21.0
    hass.config_entries.async_update_entry.assert_not_called()


# ---------------------------------------------------------------------------
# Climate-level integration (YAML setup keeps auto-save in memory)
# ---------------------------------------------------------------------------


@pytest.fixture
async def setup_thermostat_with_presets(hass: HomeAssistant):
    """Set up a heater thermostat with single-temp presets (auto-save default on)."""
    hass.states.async_set("switch.test_heater", STATE_OFF)
    hass.states.async_set(
        "sensor.test_temperature", "19", {"unit_of_measurement": "°C"}
    )
    assert await async_setup_component(
        hass,
        "climate",
        {
            "climate": {
                "platform": DOMAIN,
                "name": "test_thermostat",
                "heater": "switch.test_heater",
                "target_sensor": "sensor.test_temperature",
                PRESET_ECO: {"temperature": 18.0},
                PRESET_HOME: {"temperature": 21.0},
                PRESET_COMFORT: {"temperature": 23.0},
            }
        },
    )
    await hass.async_block_till_done()


async def test_adjustment_while_preset_active_does_not_revert(
    hass: HomeAssistant, setup_thermostat_with_presets
):
    """Changing the temperature under a preset updates the preset itself."""
    entity_id = "climate.test_thermostat"

    # Select eco (18°C).
    await hass.services.async_call(
        "climate",
        SERVICE_SET_PRESET_MODE,
        {ATTR_PRESET_MODE: PRESET_ECO, "entity_id": entity_id},
        blocking=True,
    )
    await hass.async_block_till_done()

    # Nudge eco up to 20°C from the UI.
    await hass.services.async_call(
        "climate",
        SERVICE_SET_TEMPERATURE,
        {ATTR_TEMPERATURE: 20.0, "entity_id": entity_id},
        blocking=True,
    )
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    # Still eco (auto-save takes precedence over auto-preset-selection), at 20°C.
    assert state.attributes.get(ATTR_PRESET_MODE) == PRESET_ECO
    assert state.attributes.get(ATTR_TEMPERATURE) == 20.0

    # Leave and re-enter eco: the new value must stick, not revert to 18°C.
    await hass.services.async_call(
        "climate",
        SERVICE_SET_PRESET_MODE,
        {ATTR_PRESET_MODE: PRESET_NONE, "entity_id": entity_id},
        blocking=True,
    )
    await hass.services.async_call(
        "climate",
        SERVICE_SET_PRESET_MODE,
        {ATTR_PRESET_MODE: PRESET_ECO, "entity_id": entity_id},
        blocking=True,
    )
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state.attributes.get(ATTR_PRESET_MODE) == PRESET_ECO
    assert state.attributes.get(ATTR_TEMPERATURE) == 20.0


async def test_adjustment_does_not_jump_to_other_matching_preset(
    hass: HomeAssistant, setup_thermostat_with_presets
):
    """Under eco, setting temp to a value that matches home keeps eco (auto-save)."""
    entity_id = "climate.test_thermostat"

    await hass.services.async_call(
        "climate",
        SERVICE_SET_PRESET_MODE,
        {ATTR_PRESET_MODE: PRESET_ECO, "entity_id": entity_id},
        blocking=True,
    )
    await hass.async_block_till_done()

    # 21.0 is exactly home's configured temperature.
    await hass.services.async_call(
        "climate",
        SERVICE_SET_TEMPERATURE,
        {ATTR_TEMPERATURE: 21.0, "entity_id": entity_id},
        blocking=True,
    )
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state.attributes.get(ATTR_PRESET_MODE) == PRESET_ECO
    assert state.attributes.get(ATTR_TEMPERATURE) == 21.0


async def test_auto_save_disabled_reverts_on_reapply(hass: HomeAssistant):
    """With auto-save off, re-applying the preset restores the configured value."""
    hass.states.async_set("switch.test_heater", STATE_OFF)
    hass.states.async_set(
        "sensor.test_temperature", "19", {"unit_of_measurement": "°C"}
    )
    assert await async_setup_component(
        hass,
        "climate",
        {
            "climate": {
                "platform": DOMAIN,
                "name": "test_thermostat_no_autosave",
                "heater": "switch.test_heater",
                "target_sensor": "sensor.test_temperature",
                CONF_PRESET_AUTO_SAVE: False,
                PRESET_ECO: {"temperature": 18.0},
                PRESET_HOME: {"temperature": 21.0},
            }
        },
    )
    await hass.async_block_till_done()
    entity_id = "climate.test_thermostat_no_autosave"

    await hass.services.async_call(
        "climate",
        SERVICE_SET_PRESET_MODE,
        {ATTR_PRESET_MODE: PRESET_ECO, "entity_id": entity_id},
        blocking=True,
    )
    # Set to 20°C, then back to none and eco again.
    await hass.services.async_call(
        "climate",
        SERVICE_SET_TEMPERATURE,
        {ATTR_TEMPERATURE: 20.0, "entity_id": entity_id},
        blocking=True,
    )
    await hass.services.async_call(
        "climate",
        SERVICE_SET_PRESET_MODE,
        {ATTR_PRESET_MODE: PRESET_NONE, "entity_id": entity_id},
        blocking=True,
    )
    await hass.services.async_call(
        "climate",
        SERVICE_SET_PRESET_MODE,
        {ATTR_PRESET_MODE: PRESET_ECO, "entity_id": entity_id},
        blocking=True,
    )
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    # Auto-save off: eco reverts to its configured 18°C.
    assert state.attributes.get(ATTR_TEMPERATURE) == 18.0


async def test_range_mode_adjustment_saves_into_preset(hass: HomeAssistant):
    """Range-mode (heat/cool) adjustments update both preset setpoints."""
    hass.states.async_set("switch.test_heater", STATE_OFF)
    hass.states.async_set("switch.test_cooler", STATE_OFF)
    hass.states.async_set(
        "sensor.test_temperature", "19", {"unit_of_measurement": "°C"}
    )
    assert await async_setup_component(
        hass,
        "climate",
        {
            "climate": {
                "platform": DOMAIN,
                "name": "test_thermostat_range",
                "heater": "switch.test_heater",
                "cooler": "switch.test_cooler",
                "target_sensor": "sensor.test_temperature",
                "heat_cool_mode": True,
                PRESET_ECO: {"target_temp_low": 17.0, "target_temp_high": 21.0},
                PRESET_HOME: {"target_temp_low": 18.0, "target_temp_high": 22.0},
            }
        },
    )
    await hass.async_block_till_done()
    entity_id = "climate.test_thermostat_range"

    await hass.services.async_call(
        "climate",
        SERVICE_SET_PRESET_MODE,
        {ATTR_PRESET_MODE: PRESET_ECO, "entity_id": entity_id},
        blocking=True,
    )
    await hass.services.async_call(
        "climate",
        SERVICE_SET_TEMPERATURE,
        {
            ATTR_TARGET_TEMP_LOW: 19.0,
            ATTR_TARGET_TEMP_HIGH: 24.0,
            "entity_id": entity_id,
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    # Re-apply eco and confirm the setpoints stuck.
    await hass.services.async_call(
        "climate",
        SERVICE_SET_PRESET_MODE,
        {ATTR_PRESET_MODE: PRESET_NONE, "entity_id": entity_id},
        blocking=True,
    )
    await hass.services.async_call(
        "climate",
        SERVICE_SET_PRESET_MODE,
        {ATTR_PRESET_MODE: PRESET_ECO, "entity_id": entity_id},
        blocking=True,
    )
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state.attributes.get(ATTR_PRESET_MODE) == PRESET_ECO
    assert state.attributes.get(ATTR_TARGET_TEMP_LOW) == 19.0
    assert state.attributes.get(ATTR_TARGET_TEMP_HIGH) == 24.0


# ---------------------------------------------------------------------------
# Config-entry persistence + reload-skip
# ---------------------------------------------------------------------------


async def test_config_entry_persistence_without_reload(hass: HomeAssistant):
    """A UI adjustment persists to the config entry without reloading it."""
    hass.states.async_set("switch.test_heater", STATE_OFF)
    hass.states.async_set(
        "sensor.test_temperature", "19", {"unit_of_measurement": "°C"}
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "name": "entry_thermostat",
            "heater": "switch.test_heater",
            "target_sensor": "sensor.test_temperature",
            "presets": ["eco"],
            "eco": {"temperature": 18.0},
        },
        unique_id="entry_thermostat",
        entry_id="entry_thermostat",
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entity_id = "climate.entry_thermostat"

    await hass.services.async_call(
        "climate",
        SERVICE_SET_PRESET_MODE,
        {ATTR_PRESET_MODE: PRESET_ECO, "entity_id": entity_id},
        blocking=True,
    )
    await hass.services.async_call(
        "climate",
        SERVICE_SET_TEMPERATURE,
        {ATTR_TEMPERATURE: 20.0, "entity_id": entity_id},
        blocking=True,
    )
    await hass.async_block_till_done()

    # The stored preset config now reflects the adjustment.
    assert entry.data["eco"]["temperature"] == 20.0
    # Entity still alive (no reload tore it down) and holding the new value.
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes.get(ATTR_PRESET_MODE) == PRESET_ECO
    assert state.attributes.get(ATTR_TEMPERATURE) == 20.0
    # Pending-write bookkeeping is balanced (listener consumed the write).
    pending = hass.data.get(DOMAIN, {}).get(DATA_PRESET_AUTO_SAVE_WRITES, {})
    assert pending.get(entry.entry_id, 0) == 0
