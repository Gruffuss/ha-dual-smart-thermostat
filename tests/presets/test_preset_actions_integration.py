"""Integration: climate entity drives preset actions on enter/exit."""

from homeassistant.components.climate.const import (
    DOMAIN as CLIMATE,
    PRESET_NONE,
    PRESET_SLEEP,
    HVACMode,
)
from homeassistant.setup import async_setup_component
from homeassistant.util.unit_system import METRIC_SYSTEM
import pytest

from custom_components.dual_smart_thermostat.const import DOMAIN
from custom_components.dual_smart_thermostat.managers.preset_action_manager import (
    PresetActionManager,
)
from tests import common


@pytest.mark.asyncio
async def test_climate_applies_and_restores_preset_actions(hass, monkeypatch):
    """Entering a preset calls async_apply; leaving it calls async_restore.

    Drives the real climate entity via the ``climate.set_preset_mode`` service
    and asserts the ``PresetActionManager`` transition methods fire with the
    expected enter/exit semantics. Setup mirrors the ``setup_comp_heat_presets``
    fixture in ``tests/__init__.py``.
    """
    apply_calls = []
    restore_calls = []

    orig_apply = PresetActionManager.async_apply
    orig_restore = PresetActionManager.async_restore

    async def spy_apply(self, preset_env):
        apply_calls.append(preset_env)
        return await orig_apply(self, preset_env)

    async def spy_restore(self):
        restore_calls.append(True)
        return await orig_restore(self)

    monkeypatch.setattr(PresetActionManager, "async_apply", spy_apply)
    monkeypatch.setattr(PresetActionManager, "async_restore", spy_restore)

    # --- Build a heater thermostat with a Sleep preset ---
    # The climate entity calls async_apply for any non-none preset and
    # async_restore when leaving one, so a temperature-only preset is enough to
    # exercise the enter/exit wiring. (The YAML platform schema does not yet
    # accept the ``switches``/``fan_mode`` preset keys.)
    hass.config.units = METRIC_SYSTEM
    assert await async_setup_component(
        hass,
        CLIMATE,
        {
            "climate": {
                "platform": DOMAIN,
                "name": "test",
                "cold_tolerance": 2,
                "hot_tolerance": 4,
                "heater": common.ENT_HEATER,
                "target_sensor": common.ENT_SENSOR,
                "initial_hvac_mode": HVACMode.HEAT,
                PRESET_SLEEP: {"temperature": 18},
            }
        },
    )
    await hass.async_block_till_done()

    hass.states.async_set(common.ENT_SENSOR, 20)
    await hass.async_block_till_done()

    entity_id = common.ENTITY  # "climate.test"

    # Enter Sleep -> apply called once.
    await hass.services.async_call(
        "climate",
        "set_preset_mode",
        {"entity_id": entity_id, "preset_mode": PRESET_SLEEP},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert len(apply_calls) == 1
    # The env applied on enter must be the Sleep preset env (has a temperature),
    # not the blank PRESET_NONE env.
    assert apply_calls[0].has_temp()

    # Leave to None -> restore called once.
    await hass.services.async_call(
        "climate",
        "set_preset_mode",
        {"entity_id": entity_id, "preset_mode": PRESET_NONE},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert len(restore_calls) == 1


@pytest.mark.asyncio
async def test_yaml_preset_schema_accepts_fan_mode_and_switches(hass):
    """YAML PRESET_SCHEMA must accept per-preset fan_mode + switches keys.

    Before the fix, PLATFORM_SCHEMA validation rejected ``fan_mode`` and
    ``switches`` inside a preset dict, so the entity was never created.
    After the fix both keys are optional in PRESET_SCHEMA, the entity
    registers successfully, and the Sleep preset can be selected without error.
    """
    hass.config.units = METRIC_SYSTEM
    assert await async_setup_component(
        hass,
        CLIMATE,
        {
            "climate": {
                "platform": DOMAIN,
                "name": "test",
                "cold_tolerance": 2,
                "hot_tolerance": 4,
                "heater": common.ENT_HEATER,
                "target_sensor": common.ENT_SENSOR,
                "initial_hvac_mode": HVACMode.HEAT,
                PRESET_SLEEP: {
                    "temperature": 18,
                    "fan_mode": "quiet",
                    "switches": ["input_boolean.dummy_sleep"],
                },
            }
        },
    )
    await hass.async_block_till_done()

    hass.states.async_set(common.ENT_SENSOR, 20)
    await hass.async_block_till_done()

    # Primary assertion: entity was created => schema accepted fan_mode + switches.
    state = hass.states.get(common.ENTITY)
    assert (
        state is not None
    ), "Entity was not created; PRESET_SCHEMA rejected fan_mode or switches"

    # Secondary: selecting the Sleep preset must not raise.
    await hass.services.async_call(
        "climate",
        "set_preset_mode",
        {"entity_id": common.ENTITY, "preset_mode": PRESET_SLEEP},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert hass.states.get(common.ENTITY).attributes.get("preset_mode") == PRESET_SLEEP


@pytest.mark.asyncio
async def test_preset_action_baseline_persisted_in_state_attributes(hass):
    """The pre-preset baseline is exposed in extra_state_attributes.

    When a preset with switch actions is entered, the climate entity must
    persist the prior switch state in the ``preset_action_baseline`` extra
    state attribute so the baseline survives a Home Assistant restart and
    a later exit from the preset can still revert the switch correctly.
    """
    from custom_components.dual_smart_thermostat.const import (
        ATTR_PRESET_ACTION_BASELINE,
    )

    # Create the switch in the "off" state before the entity is set up so the
    # baseline captures the real prior state.
    hass.states.async_set("input_boolean.dummy_sleep", "off")
    await hass.async_block_till_done()

    hass.config.units = METRIC_SYSTEM
    # The homeassistant domain must be set up so its turn_on/turn_off services
    # are registered — they are called when a preset's switch list is applied.
    assert await async_setup_component(hass, "homeassistant", {})
    await hass.async_block_till_done()

    assert await async_setup_component(
        hass,
        CLIMATE,
        {
            "climate": {
                "platform": DOMAIN,
                "name": "test",
                "cold_tolerance": 2,
                "hot_tolerance": 4,
                "heater": common.ENT_HEATER,
                "target_sensor": common.ENT_SENSOR,
                "initial_hvac_mode": HVACMode.HEAT,
                PRESET_SLEEP: {
                    "temperature": 18,
                    "switches": ["input_boolean.dummy_sleep"],
                },
            }
        },
    )
    await hass.async_block_till_done()

    hass.states.async_set(common.ENT_SENSOR, 20)
    await hass.async_block_till_done()

    # Select the Sleep preset — this should capture the switch's current "off"
    # state into the baseline and expose it in extra_state_attributes.
    await hass.services.async_call(
        "climate",
        "set_preset_mode",
        {"entity_id": common.ENTITY, "preset_mode": PRESET_SLEEP},
        blocking=True,
    )
    await hass.async_block_till_done()

    attrs = hass.states.get(common.ENTITY).attributes
    assert (
        ATTR_PRESET_ACTION_BASELINE in attrs
    ), f"'preset_action_baseline' key missing from state attributes: {list(attrs)}"
    baseline = attrs[ATTR_PRESET_ACTION_BASELINE]
    assert (
        baseline["switches"]["input_boolean.dummy_sleep"] == "off"
    ), f"Expected switch baseline 'off', got: {baseline}"
