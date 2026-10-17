"""Home Assistant integration tests for predictive heating."""

import asyncio
from datetime import timedelta
import json
import math
from unittest.mock import patch

from homeassistant.components.climate import HVACMode
from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store, UnsupportedStorageVersionError
from homeassistant.setup import async_setup_component
import homeassistant.util.dt as dt_util
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)
import voluptuous as vol

from custom_components.dual_smart_thermostat.const import DOMAIN
from tests import common, setup_sensor, setup_switch, setup_valve


async def _setup(hass, *, extra=None, temperature=21, units=METRIC_SYSTEM):
    hass.config.units = units
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


PREDICTIVE_KEYS = (
    "forecast_30",
    "forecast_60",
    "forecast_120",
    "forecast_180",
    "forecast_240",
    "mae_60",
    "mae_120",
    "persistence_mae_120",
    "mode",
    "alpha_rel_sd",
    "slab",
    "tau",
    "planned_on_minutes",
    "validations_120_independent",
    "learning_blocks",
)


@pytest.mark.asyncio
async def test_minute_tick_trains_and_publishes_sensors(hass, freezer):
    await _setup(hass)
    await _tick(hass, freezer, 17)
    registry = er.async_get(hass)
    heater = _heater(hass)
    assert heater._ctl.model.active().blocks >= 1
    for key in PREDICTIVE_KEYS:
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"test_predictive_{key}"
        )
        assert entity_id is not None, key
        entry = registry.async_get(entity_id)
        assert entry.entity_category == "diagnostic"
        state = hass.states.get(entity_id)
        assert state.state not in ("unavailable", "unknown"), key
        expected_unit = (
            "°C"
            if key.startswith(("forecast_", "mae_", "persistence_mae_"))
            else "min" if key in ("tau", "planned_on_minutes") else None
        )
        assert state.attributes.get("unit_of_measurement") == expected_unit
        assert state.attributes.get("state_class") == (
            None if key == "mode" else "measurement"
        )
        if key.startswith("forecast_"):
            assert state.attributes["device_class"] == "temperature"
        if key == "mode":
            assert state.attributes["device_class"] == "enum"
            assert state.attributes["options"] == ["fallback", "predictive"]
            assert "demoted_until" in state.attributes
    assert _climate(hass).extra_state_attributes["predictive_mode"] == "fallback"


@pytest.mark.asyncio
async def test_publication_failures_never_flap_valves_and_are_rate_limited(
    hass, freezer, caplog
):
    calls = await _setup(hass, temperature=20)
    hass.states.async_set(common.ENT_SWITCH, "on")
    await hass.async_block_till_done()
    calls.clear()
    with patch(
        "custom_components.dual_smart_thermostat.hvac_device.predictive_heater_device.async_dispatcher_send",
        side_effect=RuntimeError("broken sensor"),
        create=True,
    ) as publish:
        await _tick(hass, freezer, 5)
        assert publish.call_count == 5
        assert _heater(hass)._pwm.running
        assert not calls
        assert caplog.text.count("Predictive sensor publication failed") == 1
        await _tick(hass, freezer, 6)
        assert caplog.text.count("Predictive sensor publication failed") == 2


@pytest.mark.asyncio
async def test_store_round_trip_across_reload(hass, freezer, hass_storage):
    calls = setup_switch(hass, False)
    setup_sensor(hass, 21)
    hass.states.async_set("sensor.outdoor", "5")
    hass.states.async_set(common.ENT_COOLER, "off")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "name": "test",
            "heater": common.ENT_SWITCH,
            "cooler": common.ENT_COOLER,
            "target_sensor": common.ENT_SENSOR,
            "outside_sensor": "sensor.outdoor",
            "heater_control_mode": "predictive",
            "target_temp": 21,
            "initial_hvac_mode": "heat",
            "pwm_cycle_duration": 1800,
            "min_cycle_duration": {"minutes": 10},
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    heater = _heater(hass)
    await _tick(hass, freezer, 17)
    for model in heater._ctl.model.models.values():
        model.slab = 0.8
    heater._ctl.pi.integral = 12
    heater._ctl.learner.k = 3
    heater._ctl.mode = "predictive"
    heater._ctl.tracker.n120_independent = 72
    blocks = heater._ctl.model.active().blocks
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    store_key = f"{DOMAIN}.predictive.{entry.entry_id}"
    assert store_key in hass_storage
    assert hass_storage[store_key]["data"]["integral"] == 12
    calls.clear()
    await _tick(hass, freezer, 120)
    assert not calls
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    restored = _heater(hass)
    assert restored is not heater
    assert restored._ctl.pi.integral == 12
    assert restored._ctl.learner.k == 3
    assert restored._ctl.mode == "predictive"
    assert restored._ctl.tracker.n120_independent == 72
    assert restored._ctl.model.active().blocks == blocks
    for model in restored._ctl.model.models.values():
        assert model.slab == pytest.approx(0.8 * math.exp(-120 / model.tau))
    registry = er.async_get(hass)
    for key in PREDICTIVE_KEYS:
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_predictive_{key}"
        )
        assert registry.async_get(entity_id).config_entry_id == entry.entry_id


@pytest.mark.asyncio
async def test_import_predictive_state_service(hass, freezer, tmp_path, hass_storage):
    await _setup(hass)
    heater = _heater(hass)
    room = heater._ctl.to_dict()
    room.update(
        mode="pi",
        integral=17,
        k_loss=3.5,
        k_updates=4,
        model_saved_at=(dt_util.utcnow() - timedelta(minutes=60)).isoformat(),
    )
    room["slab"] = {key: 0.8 for key in room["slab"]}
    room["blocks"] = {key: 100 for key in room["blocks"]}
    room["n120_independent"] = 36
    path = tmp_path / "predictive.json"
    path.write_text(json.dumps({"version": 5, "rooms": {"test": room}}))
    data = {"entity_id": "climate.test", "path": str(path), "room": "test"}
    assert hass.services.has_service(DOMAIN, "import_predictive_state")
    with (
        patch.object(type(hass.config), "is_allowed_path", return_value=False),
        pytest.raises(HomeAssistantError, match="allowed"),
    ):
        await hass.services.async_call(
            DOMAIN, "import_predictive_state", data, blocking=True
        )
    hass.config.allowlist_external_dirs.add(str(tmp_path))
    await hass.services.async_call(
        DOMAIN, "import_predictive_state", data, blocking=True
    )
    assert heater._ctl.learner.k == 3.5
    assert heater._ctl.learner.updates == 4
    assert heater._ctl.tracker.n120_independent == 36
    for model in heater._ctl.model.models.values():
        assert model.blocks == 100
        assert model.slab == pytest.approx(0.8 * math.exp(-60 / model.tau))
    assert f"{DOMAIN}.predictive.test" in hass_storage
    assert heater._pwm.running
    path.write_text(json.dumps({"version": 4, "rooms": {"test": room}}))
    with pytest.raises(HomeAssistantError, match="v5"):
        await hass.services.async_call(
            DOMAIN, "import_predictive_state", data, blocking=True
        )
    assert heater._ctl.learner.k == 3.5


@pytest.mark.asyncio
async def test_entity_removed_stops_minute_tick(hass, freezer, hass_storage):
    calls = await _setup(hass, temperature=20)
    heater = _heater(hass)
    hass.states.async_set(common.ENT_SWITCH, "on")
    await _tick(hass, freezer, 2)
    processed = heater._ctl._processed_at
    calls.clear()
    await _climate(hass).async_remove()
    await hass.async_block_till_done()
    assert f"{DOMAIN}.predictive.test" in hass_storage
    assert [call.service for call in calls] == ["turn_off"]
    await _tick(hass, freezer, 61)
    assert heater._ctl._processed_at == processed
    assert [call.service for call in calls] == ["turn_off"]


@pytest.mark.asyncio
async def test_reset_predictive_learning(hass, freezer, hass_storage):
    await _setup(hass)
    await _tick(hass, freezer, 17)
    assert _heater(hass)._ctl.model.active().blocks > 0
    await hass.services.async_call(
        DOMAIN, "reset_heating_power", {"entity_id": "climate.test"}, blocking=True
    )
    assert _heater(hass)._ctl.model.active().blocks == 0
    assert _heater(hass)._ctl.mode == "fallback"
    assert hass_storage[f"{DOMAIN}.predictive.test"]["data"]["blocks"]["120"] == 0


@pytest.mark.asyncio
async def test_idle_temperature_sensor_stall_uses_feedforward(hass, freezer):
    await _setup(hass, temperature=23)
    heater = _heater(hass)
    assert heater.duty_cycle == 0
    await _climate(hass)._async_sensor_not_responding(dt_util.utcnow())
    await _tick(hass, freezer)
    assert heater._ctl._last_inputs.temp is None
    assert heater._ctl.pi.output == 32


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "cool", "fan_only", "dry"])
async def test_non_heating_modes_withhold_demand(hass, freezer, mode):
    hass.states.async_set(common.ENT_COOLER, "off")
    await _setup(hass, temperature=20, extra={"cooler": common.ENT_COOLER})
    heater = _heater(hass)
    # Exercise the parent dispatch setter even for modes supplied by an outer
    # multi-device wrapper; every non-heating dispatch must stop this heater.
    _climate(hass).hvac_device.hvac_mode = HVACMode(mode)
    await _tick(hass, freezer, 17)
    assert not heater._ctl._last_inputs.demand
    assert heater._ctl.model.active().blocks >= 1, "off periods still teach the model"
    assert not heater._pwm.running


@pytest.mark.asyncio
async def test_store_delayed_save_and_immediate_gate_save(hass, freezer, hass_storage):
    await _setup(hass, temperature=23, extra={"predictive_gate": "on"})
    key = f"{DOMAIN}.predictive.test"
    await _tick(hass, freezer, 6)
    assert key in hass_storage, "minute callbacks must not perpetually postpone saving"
    setup_sensor(hass, 20)
    await hass.async_block_till_done()
    await _tick(hass, freezer)
    assert hass_storage[key]["data"]["mode"] == "fallback"
    assert hass_storage[key]["data"]["demoted_until"] is not None


@pytest.mark.asyncio
async def test_invalid_store_starts_fresh(hass, freezer, hass_storage, caplog):
    hass_storage[f"{DOMAIN}.predictive.test"] = {
        "version": 1,
        "data": {"saved_at": "not a date", "integral": float("nan")},
    }
    await _setup(hass)
    assert _heater(hass)._ctl.model.active().blocks == 0
    assert "starting fresh" in caplog.text


@pytest.mark.asyncio
async def test_tick_jitter_and_missing_outdoor_invalidate(hass, freezer):
    await _setup(hass)
    heater = _heater(hass)
    freezer.tick(timedelta(seconds=70))
    common.async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert not heater._ctl._last_inputs.valid
    await _tick(hass, freezer, 2)
    assert heater._ctl._last_inputs.valid
    # Last-known outdoor is retained after its sensor becomes unavailable.
    hass.states.async_set("sensor.outdoor", "unavailable")
    await hass.async_block_till_done()
    await _tick(hass, freezer)
    assert heater._ctl._last_inputs.outdoor == 5
    assert heater._ctl._last_inputs.valid


@pytest.mark.asyncio
async def test_floor_protection_overrides_pwm_and_keeps_validity(hass, freezer):
    hass.states.async_set("sensor.floor", "22")
    calls = await _setup(
        hass,
        temperature=20,
        extra={
            "floor_sensor": "sensor.floor",
            "min_floor_temp": 20,
            "max_floor_temp": 28,
        },
    )
    heater = _heater(hass)
    hass.states.async_set("sensor.floor", "30")
    await hass.async_block_till_done()
    await _tick(hass, freezer)
    assert not heater._pwm.running
    assert heater._ctl._last_inputs.valid
    calls.clear()
    hass.states.async_set("sensor.floor", "18")
    await hass.async_block_till_done()
    hass.states.async_set(common.ENT_SWITCH, "on")
    await _tick(hass, freezer)
    assert any(c.service == "turn_on" for c in calls)
    assert not heater._pwm.running
    assert heater._ctl._last_inputs.valve_on
    assert heater._ctl._last_inputs.valid
    heater._cycle_started_min -= 7 * 24 * 60
    with patch.object(
        heater._ctl, "forecasts", wraps=heater._ctl.forecasts
    ) as forecasts:
        heater._publish()
        assert (
            forecasts.call_args.args[1] < 30
        ), "floor override must not build an unbounded forecast"


@pytest.mark.asyncio
async def test_sensor_callback_failure_is_rate_limited(hass, freezer, caplog):
    await _setup(hass)
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, "test_predictive_slab")
    sensor = hass.data["entity_components"]["sensor"].get_entity(entity_id)
    with patch.object(
        sensor, "async_write_ha_state", side_effect=ValueError("invalid sensor state")
    ):
        await _tick(hass, freezer, 3)
        failures = [
            r
            for r in caplog.records
            if "invalid sensor state" in r.getMessage()
            or (r.exc_info and "invalid sensor state" in str(r.exc_info[1]))
        ]
        assert len(failures) == 1
        assert _heater(hass)._pwm.running


@pytest.mark.asyncio
async def test_predictive_uses_celsius_with_fahrenheit_ha(hass, freezer):
    await _setup(
        hass, temperature=68, units=US_CUSTOMARY_SYSTEM, extra={"target_temp": 69.8}
    )
    hass.states.async_set("sensor.outdoor", "41")
    await hass.async_block_till_done()
    await _tick(hass, freezer)
    inp = _heater(hass)._ctl._last_inputs
    assert inp.temp == pytest.approx(20)
    assert inp.setpoint == pytest.approx(21)
    assert inp.outdoor == pytest.approx(5)


@pytest.mark.asyncio
async def test_floor_override_forecasts_reuse_the_cached_path(hass, freezer):
    hass.states.async_set("sensor.floor", "18")
    await _setup(
        hass,
        temperature=21,
        extra={
            "floor_sensor": "sensor.floor",
            "min_floor_temp": 20,
        },
    )
    from custom_components.dual_smart_thermostat.control.predictive import forecast

    with patch(
        "custom_components.dual_smart_thermostat.control.predictive.forecast",
        wraps=forecast,
    ) as simulate:
        await _tick(hass, freezer, 5)
        assert simulate.call_count == 1


@pytest.mark.asyncio
async def test_cycle_boundary_saves_gate_switch_immediately(
    hass, freezer, hass_storage
):
    await _setup(hass, temperature=23, extra={"predictive_gate": "on"})
    await _tick(hass, freezer, 6)
    key = f"{DOMAIN}.predictive.test"
    assert hass_storage[key]["data"]["mode"] == "predictive"
    setup_sensor(hass, 20)
    await hass.async_block_till_done()
    await _heater(hass)._pwm.async_restart()
    await hass.async_block_till_done()
    assert hass_storage[key]["data"]["mode"] == "fallback"


@pytest.mark.parametrize(
    ("extra", "error"),
    [
        ({"outside_sensor": None}, "predictive_requires_outside_sensor"),
        ({"pwm_cycle_duration": 1799}, "predictive_cycle_whole_minutes"),
        ({"pwm_cycle_duration": 1200}, "predictive_cycle_too_short"),
    ],
)
def test_yaml_predictive_dependencies_are_validated(hass, extra, error):
    from custom_components.dual_smart_thermostat.climate import PLATFORM_SCHEMA

    config = {
        "platform": DOMAIN,
        "name": "test",
        "heater": common.ENT_SWITCH,
        "target_sensor": common.ENT_SENSOR,
        "outside_sensor": "sensor.outdoor",
        "heater_control_mode": "predictive",
        "pwm_cycle_duration": 1800,
        "min_cycle_duration": {"minutes": 10},
        **extra,
    }
    if config.get("outside_sensor") is None:
        config.pop("outside_sensor")
    with pytest.raises(vol.Invalid, match=error):
        PLATFORM_SCHEMA(config)


@pytest.mark.asyncio
async def test_heat_cool_preserves_cooler_minimum_cycle(hass, freezer):
    hass.states.async_set(common.ENT_COOLER, "off")
    calls = await _setup(
        hass,
        temperature=27,
        extra={
            "cooler": common.ENT_COOLER,
            "heat_cool_mode": True,
            "target_temp_low": 21,
            "target_temp_high": 25,
            "initial_hvac_mode": HVACMode.HEAT_COOL,
        },
    )
    hass.states.async_set(common.ENT_COOLER, "on")
    await hass.async_block_till_done()
    calls.clear()
    setup_sensor(hass, 23)
    await hass.async_block_till_done()
    assert not [
        c
        for c in calls
        if c.service == "turn_off" and c.data["entity_id"] == common.ENT_COOLER
    ]
    assert not _heater(hass)._pwm.running


@pytest.mark.asyncio
async def test_controller_clock_does_not_follow_wall_clock_adjustments(hass, freezer):
    await _setup(hass)
    await _tick(hass, freezer, 2)
    heater = _heater(hass)
    before = heater._inputs().now_min
    with patch(
        "homeassistant.util.dt.utcnow",
        return_value=dt_util.utcnow() - timedelta(hours=1),
    ):
        assert heater._inputs().now_min >= before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("devices", "mode"),
    [
        ({"fan": "switch.fan"}, HVACMode.FAN_ONLY),
        ({"dryer": "switch.dryer"}, HVACMode.DRY),
        ({"dryer": "switch.dryer", "cooler": "switch.cooler"}, HVACMode.DRY),
    ],
)
async def test_composite_non_heating_mode_stops_predictive(
    hass, freezer, devices, mode
):
    for entity_id in devices.values():
        hass.states.async_set(entity_id, "off")
    hass.states.async_set("sensor.humidity", "70")
    calls = await _setup(
        hass,
        temperature=20,
        extra={**devices, "humidity_sensor": "sensor.humidity", "target_humidity": 50},
    )
    heater = _heater(hass)
    assert heater._pwm.running
    hass.states.async_set(common.ENT_SWITCH, "on")
    await hass.async_block_till_done()
    await hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {"entity_id": "climate.test", "hvac_mode": mode},
        blocking=True,
    )
    assert not heater._inputs().demand
    assert not heater._pwm.running
    hass.states.async_set(common.ENT_SWITCH, "off")
    await hass.async_block_till_done()
    calls.clear()
    await _tick(hass, freezer, 61)
    assert not [
        call
        for call in calls
        if call.service == "turn_on" and call.data.get("entity_id") == common.ENT_SWITCH
    ]
    await hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {"entity_id": "climate.test", "hvac_mode": HVACMode.HEAT},
        blocking=True,
    )
    assert heater._inputs().demand
    assert heater._pwm.running


@pytest.mark.asyncio
@pytest.mark.parametrize("control_mode", ["predictive", "ai", "tpi"])
async def test_floor_forced_on_then_off_closes_valve(hass, control_mode):
    hass.states.async_set("sensor.floor", "18")
    calls = await _setup(
        hass,
        temperature=20,
        extra={
            "heater_control_mode": control_mode,
            "floor_sensor": "sensor.floor",
            "min_floor_temp": 20,
            "max_floor_temp": 28,
        },
    )
    assert any(call.service == "turn_on" for call in calls)
    assert not _heater(hass)._pwm.running
    hass.states.async_set(common.ENT_SWITCH, "on")
    await hass.async_block_till_done()
    calls.clear()
    await hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {"entity_id": "climate.test", "hvac_mode": HVACMode.OFF},
        blocking=True,
    )
    assert any(call.service == "turn_off" for call in calls)


@pytest.mark.asyncio
async def test_cooler_selection_closes_valve_after_cancel(hass):
    calls = await _setup(hass, temperature=20)
    heater = _heater(hass)
    heater._pwm.cancel()
    hass.states.async_set(common.ENT_SWITCH, "on")
    heater.set_cooler_selected(True)
    await hass.async_block_till_done()
    calls.clear()
    await heater.async_control_hvac()
    assert [call.service for call in calls] == ["turn_off"]


@pytest.mark.asyncio
async def test_pwm_planner_error_closes_valve_and_retries(hass, freezer, caplog):
    calls = await _setup(hass, temperature=20)
    heater = _heater(hass)
    hass.states.async_set(common.ENT_SWITCH, "on")
    await hass.async_block_till_done()
    calls.clear()
    with patch.object(
        heater._pwm, "_compute_duty", side_effect=[RuntimeError("bad plan"), 100]
    ) as compute:
        await heater._pwm.async_restart()
        assert [call.service for call in calls] == ["turn_off"]
        assert heater._pwm.last_duty == 0
        assert heater._pwm.running
        assert "bad plan" in caplog.text
        hass.states.async_set(common.ENT_SWITCH, "off")
        await hass.async_block_till_done()
        calls.clear()
        await _tick(hass, freezer, 30)
        assert compute.call_count == 2
        assert any(call.service == "turn_on" for call in calls)
        assert heater._pwm.last_duty == 100


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        OSError("unreadable predictive store"),
        UnsupportedStorageVersionError("test", 99, 1),
    ],
    ids=["read-error", "unsupported-version"],
)
async def test_storage_read_error_starts_fresh_and_registers_tick(
    hass, freezer, caplog, error
):
    original_load = Store.async_load

    async def load(store):
        if store.key.startswith(f"{DOMAIN}.predictive."):
            raise error
        return await original_load(store)

    with patch.object(Store, "async_load", load):
        await _setup(hass)
    heater = _heater(hass)
    await _tick(hass, freezer, 2)
    assert heater._ctl._processed_at is not None
    assert heater._ctl._last_inputs.valid
    assert "Cannot load predictive state; starting fresh" in caplog.text


@pytest.mark.asyncio
async def test_valve_open_state_is_used_for_learning(hass, freezer):
    setup_valve(hass, True)
    await _setup(hass, temperature=20, extra={"heater": common.ENT_VALVE})
    await _tick(hass, freezer)
    assert _heater(hass)._ctl._last_inputs.valve_on


@pytest.mark.asyncio
@pytest.mark.parametrize("control_mode", ["predictive", "ai", "tpi"])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("valve_state", ["on", "off"])
async def test_humidity_emergency_stops_pwm_until_recovery(
    hass, freezer, control_mode, nested, valve_state
):
    extra = {
        "heater_control_mode": control_mode,
        "humidity_sensor": "sensor.humidity",
        "sensor_stale_duration": {"hours": 2},
    }
    hass.states.async_set("sensor.humidity", "50")
    if nested:
        extra.update({"cooler": common.ENT_COOLER, "dryer": "switch.dryer"})
        hass.states.async_set(common.ENT_COOLER, "off")
        hass.states.async_set("switch.dryer", "off")
    calls = await _setup(hass, temperature=19, extra=extra)
    heater = _heater(hass)
    assert heater._pwm.running
    hass.states.async_set(common.ENT_SWITCH, valve_state)
    await hass.async_block_till_done()
    await _climate(hass)._async_humidity_sensor_not_responding()
    assert not heater._pwm.running
    hass.states.async_set(common.ENT_SWITCH, "off")
    await hass.async_block_till_done()
    calls.clear()
    await _tick(hass, freezer, 31)
    assert not [
        c
        for c in calls
        if c.service == "turn_on" and c.data["entity_id"] == common.ENT_SWITCH
    ]
    hass.states.async_set("sensor.humidity", "51")
    await hass.async_block_till_done()
    assert heater._pwm.running


@pytest.mark.asyncio
@pytest.mark.parametrize("control_mode", ["ai", "tpi"])
@pytest.mark.parametrize("selection", ["band", "cooler", "off"])
async def test_heat_cool_deselection_stops_non_predictive_pwm(
    hass, freezer, control_mode, selection
):
    hass.states.async_set(common.ENT_COOLER, "off")
    calls = await _setup(
        hass,
        temperature=19,
        extra={
            "heater_control_mode": control_mode,
            "cooler": common.ENT_COOLER,
            "heat_cool_mode": True,
            "target_temp_low": 21,
            "target_temp_high": 25,
        },
    )
    await _climate(hass).async_set_hvac_mode(HVACMode.HEAT_COOL)
    heater = _heater(hass)
    assert heater._pwm.running
    # During the off segment neither switch is active, but a future boundary exists.
    hass.states.async_set(common.ENT_SWITCH, "off")
    if selection == "off":
        await _climate(hass).async_set_hvac_mode(HVACMode.OFF)
    else:
        setup_sensor(hass, 23 if selection == "band" else 27)
        await hass.async_block_till_done()
    assert not heater._pwm.running
    calls.clear()
    await _tick(hass, freezer, 31)
    assert not [
        c
        for c in calls
        if c.service == "turn_on" and c.data["entity_id"] == common.ENT_SWITCH
    ]


@pytest.mark.asyncio
async def test_cold_floor_preserves_cooler_minimum_cycle(hass, freezer):
    hass.states.async_set(common.ENT_COOLER, "off")
    hass.states.async_set("sensor.floor", "22")
    calls = await _setup(
        hass,
        temperature=27,
        extra={
            "cooler": common.ENT_COOLER,
            "heat_cool_mode": True,
            "target_temp_low": 21,
            "target_temp_high": 25,
            "initial_hvac_mode": HVACMode.HEAT_COOL,
            "floor_sensor": "sensor.floor",
            "min_floor_temp": 20,
        },
    )
    hass.states.async_set(common.ENT_COOLER, "on")
    await hass.async_block_till_done()
    calls.clear()
    hass.states.async_set("sensor.floor", "18")
    await hass.async_block_till_done()
    assert not [
        c
        for c in calls
        if c.service == "turn_off" and c.data["entity_id"] == common.ENT_COOLER
    ]
    await _tick(hass, freezer, 11)
    await _climate(hass)._async_control_climate()
    assert any(
        c.service == "turn_off" and c.data["entity_id"] == common.ENT_COOLER
        for c in calls
    )


@pytest.mark.asyncio
async def test_removal_during_store_load_registers_no_callbacks(hass):
    await _setup(hass, extra={"openings": ["binary_sensor.window"]})
    heater = _heater(hass)
    entered, release = asyncio.Event(), asyncio.Event()

    async def load():
        entered.set()
        await release.wait()

    with patch.object(heater._store, "async_load", load):
        startup = hass.async_create_task(heater.async_on_startup())
        await entered.wait()
        await _climate(hass).async_remove()
        with (
            patch(
                "custom_components.dual_smart_thermostat.hvac_device.predictive_heater_device.async_track_time_interval"
            ) as timer,
            patch(
                "custom_components.dual_smart_thermostat.hvac_device.predictive_heater_device.async_track_state_change_event"
            ) as listener,
        ):
            release.set()
            await startup
            timer.assert_not_called()
            listener.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("unique_id", [None, "Zone/One Thermostat"])
async def test_yaml_store_key_is_path_safe(hass, freezer, hass_storage, unique_id):
    extra = {"name": "Kitchen/Living Room"}
    if unique_id:
        extra["unique_id"] = unique_id
    await _setup(hass, extra=extra)
    climate = hass.data["entity_components"]["climate"].get_entity(
        "climate.kitchen_living_room"
    )
    heater = climate._find_pwm_heater()
    key = "zone_one_thermostat" if unique_id else "kitchen_living_room"
    assert heater._store.key == f"{DOMAIN}.predictive.{key}"
    await _tick(hass, freezer)
    registry = er.async_get(hass)
    sensor = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{unique_id or extra['name']}_predictive_slab"
    )
    assert hass.states.get(sensor).state != "unknown"
    await heater.async_save()
    assert heater._store.key in hass_storage


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("units", "display"), [(METRIC_SYSTEM, "25.0"), (US_CUSTOMARY_SYSTEM, "77.0")]
)
async def test_predictive_sensor_restores_native_value_before_first_tick(
    hass, units, display
):
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State("sensor.test_predictive_forecast_30", display),
                {"native_value": 25.0, "native_unit_of_measurement": "°C"},
            )
        ],
    )
    await _setup(hass, units=units)
    assert hass.states.get("sensor.test_predictive_forecast_30").state == display


@pytest.mark.asyncio
async def test_switching_from_predictive_removes_sensor_registry_entries(hass, freezer):
    setup_switch(hass, False)
    setup_sensor(hass, 21)
    hass.states.async_set("sensor.outdoor", "5")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "name": "test",
            "heater": common.ENT_SWITCH,
            "target_sensor": common.ENT_SENSOR,
            "outside_sensor": "sensor.outdoor",
            "heater_control_mode": "predictive",
            "pwm_cycle_duration": 1800,
            "min_cycle_duration": {"minutes": 10},
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    ids = [f"{entry.entry_id}_predictive_{key}" for key in PREDICTIVE_KEYS]
    assert all(registry.async_get_entity_id("sensor", DOMAIN, key) for key in ids)
    assert await hass.config_entries.async_unload(entry.entry_id)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "heater_control_mode": "tpi"}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert all(
        registry.async_get_entity_id("sensor", DOMAIN, key) is None for key in ids
    )
    assert registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_hvac_action_reason"
    )
