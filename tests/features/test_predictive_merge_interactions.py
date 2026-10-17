"""Exercise predictive control with preset persistence and wrapped devices."""

from unittest.mock import patch

from homeassistant.components.climate import HVACMode
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dual_smart_thermostat.const import (
    DATA_PRESET_AUTO_SAVE_WRITES,
    DOMAIN,
)
from tests import common, setup_sensor, setup_switch
from tests.features.test_predictive_heating import (  # noqa: F401
    _climate,
    _heater,
    _setup,
    _tick,
    cleanup_predictive_entities,
)
from tests.test_wrapped_climate_cooler import (
    WRAPPED_AC,
    _register_ac_handlers,
    _set_ac_state,
)


@pytest.mark.asyncio
async def test_predictive_preset_save_restarts_once_without_reload(hass, freezer):
    """Persist a changed preset, keep learning alive, and allow later options reload."""
    setup_switch(hass, False)
    setup_sensor(hass, 23)
    hass.states.async_set("sensor.outdoor", "5")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "name": "test",
            "heater": common.ENT_SWITCH,
            "target_sensor": common.ENT_SENSOR,
            "outside_sensor": "sensor.outdoor",
            "heater_control_mode": "predictive",
            "initial_hvac_mode": HVACMode.HEAT,
            "target_temp": 21,
            "eco": {"temperature": 21},
        },
        options={"eco": {"temperature": 22}},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    thermostat = _climate(hass)
    await thermostat.async_set_preset_mode("eco")
    await hass.async_block_till_done()
    heater = _heater(hass)
    await _tick(hass, freezer, 2)
    assert heater._pwm.running
    with (
        patch.object(
            heater._pwm, "async_restart", wraps=heater._pwm.async_restart
        ) as restart,
        patch.object(
            hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
        ) as reload,
    ):
        await thermostat.async_set_temperature(temperature=25)
        await hass.async_block_till_done()
        assert entry.data["eco"]["temperature"] == 25
        assert entry.options["eco"]["temperature"] == 25
        assert thermostat.preset_mode == "eco"
        assert heater.duty_cycle == 100
        assert _heater(hass) is heater
        restart.assert_awaited_once()
        reload.assert_not_awaited()

        await thermostat.async_set_temperature(temperature=25)
        await thermostat.async_set_preset_mode("eco")
        await _tick(hass, freezer, 2)
        restart.assert_awaited_once()
        reload.assert_not_awaited()
        assert hass.data[DOMAIN][DATA_PRESET_AUTO_SAVE_WRITES][entry.entry_id] == 0

        result = await hass.config_entries.options.async_init(entry.entry_id)
        await hass.config_entries.options.async_configure(
            result["flow_id"], {"target_temp": 24}
        )
        await hass.async_block_till_done()
        reload.assert_awaited_once_with(entry.entry_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("range_mode", [False, True])
async def test_predictive_wrapped_ac_target_push(hass, range_mode):
    """A running wrapped AC receives the new upper/single target immediately."""
    _set_ac_state(hass)
    await _setup(
        hass,
        temperature=28,
        extra={
            "cooler": WRAPPED_AC,
            "heat_cool_mode": range_mode,
            "initial_hvac_mode": HVACMode.HEAT_COOL if range_mode else HVACMode.COOL,
            "target_temp": 24,
            "target_temp_low": 21,
            "target_temp_high": 24,
        },
    )
    calls = _register_ac_handlers(hass)
    await _climate(hass)._async_control_climate(force=True)
    await hass.async_block_till_done()
    assert hass.states.get(WRAPPED_AC).state == HVACMode.COOL
    calls["set_temperature"].clear()
    kwargs = (
        {"target_temp_low": 20, "target_temp_high": 23}
        if range_mode
        else {"temperature": 23}
    )
    await _climate(hass).async_set_temperature(**kwargs)
    await hass.async_block_till_done()
    assert calls["set_temperature"][-1].data["temperature"] == 23
    assert not _heater(hass)._pwm.running


@pytest.mark.asyncio
async def test_predictive_heat_cool_with_heater_fan(hass, freezer):
    """The heater fan wrapper must preserve early heating and cooling exclusion."""
    hass.states.async_set(common.ENT_COOLER, "off")
    hass.states.async_set("switch.fan", "off")
    await _setup(
        hass,
        temperature=21.1,
        extra={
            "cooler": common.ENT_COOLER,
            "fan": "switch.fan",
            "fan_on_with_heater": True,
            "heat_cool_mode": True,
            "initial_hvac_mode": HVACMode.HEAT_COOL,
            "target_temp_low": 21,
            "target_temp_high": 25,
            "predictive_gate": "on",
        },
    )
    heater = _heater(hass)
    assert heater._pwm.running
    assert heater.duty_cycle > 0
    setup_sensor(hass, 27)
    await hass.async_block_till_done()
    assert not heater._pwm.running
    await _tick(hass, freezer)
    assert not heater._pwm.running


@pytest.mark.asyncio
@pytest.mark.parametrize("control_mode", ["ai", "tpi"])
@pytest.mark.parametrize("selection", ["fan_only", "cooler"])
async def test_wrapped_pwm_deselection_cancels_off_segment(
    hass, freezer, control_mode, selection
):
    """A fan wrapper must stop a PWM timer even when its valve is already off."""
    hass.states.async_set("switch.fan", "off")
    hass.states.async_set(common.ENT_COOLER, "off")
    calls = await _setup(
        hass,
        temperature=20.5,
        extra={
            "heater_control_mode": control_mode,
            "fan": "switch.fan",
            "fan_on_with_heater": True,
            "cooler": common.ENT_COOLER,
            "heat_cool_mode": True,
            "initial_hvac_mode": HVACMode.HEAT_COOL,
            "target_temp_low": 21,
            "target_temp_high": 25,
        },
    )
    heater = _heater(hass)
    assert heater._pwm.running
    hass.states.async_set(common.ENT_SWITCH, "off")
    await hass.async_block_till_done()
    if selection == "fan_only":
        await _climate(hass).async_set_hvac_mode(HVACMode.FAN_ONLY)
    else:
        setup_sensor(hass, 27)
        await hass.async_block_till_done()
    assert not heater._pwm.running
    calls.clear()
    await _tick(hass, freezer, 31)
    assert not [
        call
        for call in calls
        if call.service == "turn_on" and call.data.get("entity_id") == common.ENT_SWITCH
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("control_mode", ["ai", "tpi"])
async def test_heat_cool_shared_fan_stays_on_for_heating(hass, control_mode):
    """Starting heat must not turn off the fan shared with the idle cooler."""
    hass.states.async_set("switch.fan", "off")
    hass.states.async_set(common.ENT_COOLER, "off")
    calls = await _setup(
        hass,
        temperature=23,
        extra={
            "heater_control_mode": control_mode,
            "fan": "switch.fan",
            "fan_on_with_heater": True,
            "cooler": common.ENT_COOLER,
            "heat_cool_mode": True,
            "initial_hvac_mode": HVACMode.HEAT_COOL,
            "target_temp_low": 21,
            "target_temp_high": 25,
        },
    )
    hass.states.async_set("switch.fan", "on")
    await hass.async_block_till_done()
    calls.clear()
    setup_sensor(hass, 20)
    await hass.async_block_till_done()
    assert _heater(hass)._pwm.running
    assert not [
        call
        for call in calls
        if call.service == "turn_off" and call.data.get("entity_id") == "switch.fan"
    ]
