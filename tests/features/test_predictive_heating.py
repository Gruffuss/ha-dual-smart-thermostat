"""Home Assistant integration tests for predictive heating."""

from datetime import timedelta

from homeassistant.components.climate import HVACMode
from homeassistant.setup import async_setup_component
from homeassistant.util.unit_system import METRIC_SYSTEM
import pytest

from custom_components.dual_smart_thermostat.const import DOMAIN
from tests import common, setup_sensor, setup_switch


async def _setup(hass, *, extra=None, temperature=21):
    hass.config.units = METRIC_SYSTEM
    calls = setup_switch(hass, False)
    hass.states.async_set("sensor.outdoor", "5")
    assert await async_setup_component(
        hass,
        "climate",
        {
            "climate": {
                "platform": DOMAIN,
                "name": "test",
                "heater": common.ENT_SWITCH,
                "target_sensor": common.ENT_SENSOR,
                "outside_sensor": "sensor.outdoor",
                "heater_control_mode": "predictive",
                "pwm_cycle_duration": 1800,
                "min_cycle_duration": {"minutes": 10},
                "target_temp": 21,
                "initial_hvac_mode": HVACMode.HEAT,
                **(extra or {}),
            }
        },
    )
    await hass.async_block_till_done()
    setup_sensor(hass, temperature)
    await hass.async_block_till_done()
    return calls


def _climate(hass):
    return hass.data["entity_components"]["climate"].get_entity("climate.test")


def _heater(hass):
    return _climate(hass)._find_pwm_heater()


async def _tick(hass, freezer, minutes=1):
    for _ in range(minutes):
        freezer.tick(timedelta(minutes=1))
        common.async_fire_time_changed(hass)
        await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_predictive_mode_creates_device(hass):
    await _setup(hass)
    assert type(_heater(hass)).__name__ == "PredictiveHeaterDevice"
    assert _heater(hass)._ctl.settings.cycle_minutes == 30
    assert _heater(hass)._ctl.settings.min_on_minutes == 10


@pytest.mark.asyncio
async def test_setpoint_change_restarts_cycle(hass, freezer):
    await _setup(hass, temperature=23)
    assert _heater(hass) is not None
    assert _heater(hass).duty_cycle == 0
    await _tick(hass, freezer, 2)
    await hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": "climate.test", "temperature": 25},
        blocking=True,
    )
    assert _heater(hass).duty_cycle == 100


@pytest.mark.asyncio
async def test_cold_floor_restarts_cycle_full_on(hass, freezer):
    await _setup(hass, temperature=23, extra={"predictive_gate": "on"})
    heater = _heater(hass)
    assert heater is not None
    setup_sensor(hass, 20)
    await hass.async_block_till_done()
    await _tick(hass, freezer)
    assert heater.duty_cycle == 100
    assert heater._ctl.mode == "fallback"
    assert heater._ctl.demoted_until_min > 1400


@pytest.mark.asyncio
async def test_heat_cool_early_start_inside_band(hass, freezer):
    hass.states.async_set(common.ENT_COOLER, "off")
    await _setup(
        hass,
        temperature=21.1,
        extra={
            "cooler": common.ENT_COOLER,
            "heat_cool_mode": True,
            "target_temp_low": 21,
            "target_temp_high": 25,
            "initial_hvac_mode": HVACMode.HEAT_COOL,
            "predictive_gate": "on",
        },
    )
    heater = _heater(hass)
    assert heater._pwm.running
    assert heater.duty_cycle > 0, (
        heater._ctl._last_inputs,
        heater.hvac_mode,
        heater._ctl.mode,
    )
    await _tick(hass, freezer)
    assert heater._ctl._last_inputs.demand
    # A real cooling request must still win, even with a hot slab/heater on.
    hass.states.async_set(common.ENT_SWITCH, "on")
    setup_sensor(hass, 27)
    await hass.async_block_till_done()
    assert not heater._pwm.running
    await _tick(hass, freezer)
    assert not heater._pwm.running


@pytest.mark.asyncio
async def test_opening_invalidates_and_stops(hass, freezer):
    hass.states.async_set("binary_sensor.window", "off")
    await _setup(hass, temperature=20, extra={"openings": ["binary_sensor.window"]})
    heater = _heater(hass)
    await _tick(hass, freezer, 2)
    assert heater._ctl._last_inputs.valid
    hass.states.async_set("binary_sensor.window", "on")
    await hass.async_block_till_done()
    await _tick(hass, freezer)
    assert not heater._ctl._last_inputs.valid
    assert not heater._pwm.running
    assert not heater._ctl.tracker.pending
    hass.states.async_set("binary_sensor.window", "off")
    await hass.async_block_till_done()
    await _tick(hass, freezer, 14)
    assert not heater._ctl._last_inputs.valid
    await _tick(hass, freezer, 2)
    assert heater._ctl._last_inputs.valid


@pytest.mark.asyncio
async def test_valve_maintenance_invalidates(hass, freezer):
    calls = await _setup(hass, temperature=20)
    heater = _heater(hass)
    await _tick(hass, freezer, 2)
    _climate(hass)._set_valve_maintenance_active(True)
    calls.clear()
    await _tick(hass, freezer, 31)
    assert not heater._ctl._last_inputs.valid
    assert not heater._ctl._last_inputs.demand
    assert not heater._pwm.running
    assert not calls, "maintenance owns the valve, including at PWM boundaries"
    _climate(hass)._set_valve_maintenance_active(False)
    await _tick(hass, freezer)
    assert heater._pwm.running


@pytest.mark.asyncio
async def test_ac_active_invalidates_learning(hass, freezer):
    hass.states.async_set("climate.ac", "off", {"hvac_modes": ["off", "cool"]})
    await _setup(hass, temperature=20, extra={"cooler": "climate.ac"})
    heater = _heater(hass)
    await _tick(hass, freezer, 2)
    assert heater._ctl._last_inputs.valid
    hass.states.async_set("climate.ac", "cool", {"hvac_modes": ["off", "cool"]})
    await hass.async_block_till_done()
    await _tick(hass, freezer)
    assert not heater._ctl._last_inputs.valid
    assert heater._ctl._last_inputs.demand, "AC validity is not a heating interlock"
    assert not heater._ctl.tracker.pending
    hass.states.async_set("climate.ac", "off", {"hvac_modes": ["off", "cool"]})
    await hass.async_block_till_done()
    await _tick(hass, freezer, 29)
    assert not heater._ctl._last_inputs.valid
    await _tick(hass, freezer, 2)
    assert heater._ctl._last_inputs.valid


@pytest.mark.asyncio
async def test_temperature_fault_uses_feedforward(hass, freezer):
    await _setup(hass, temperature=20)
    heater = _heater(hass)
    await _tick(hass, freezer)
    _climate(hass)._sensor_stalled = True
    await _tick(hass, freezer)
    assert heater._ctl._last_inputs.temp is None
    assert not heater._ctl._last_inputs.valid
    assert heater._ctl.pi.output == 32
    assert heater._pwm.running
