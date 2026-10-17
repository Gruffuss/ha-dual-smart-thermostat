"""Behavioral tests for valve exercise holds and lifecycle."""

import asyncio
from datetime import timedelta

from homeassistant.components.climate import HVACMode
from homeassistant.setup import async_setup_component
import pytest
import voluptuous as vol

from custom_components.dual_smart_thermostat.climate import PLATFORM_SCHEMA
from custom_components.dual_smart_thermostat.const import DOMAIN
from custom_components.dual_smart_thermostat.managers.valve_maintenance_manager import (
    ValveMaintenanceManager,
)
from tests import common, setup_switch


async def flush():
    for _ in range(30):
        await asyncio.sleep(0)


async def tick(hass, freezer, seconds):
    freezer.tick(timedelta(seconds=seconds))
    common.async_fire_time_changed(hass)
    await flush()


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", [None, 2])
async def test_maintenance_holds_then_closes(hass, freezer, minutes):
    """The configured hold defaults to six minutes and suspends all control."""
    calls = setup_switch(hass, False)
    suspends = []
    kwargs = {} if minutes is None else {"open_minutes": minutes}
    manager = ValveMaintenanceManager(
        hass, common.ENT_SWITCH, 7, suspends.append, **kwargs
    )
    task = asyncio.create_task(manager.async_run_cycle())
    await flush()
    assert [c.service for c in calls] == ["turn_on"]
    assert suspends == [True]
    await tick(hass, freezer, (minutes or 6) * 60 - 1)
    assert [c.service for c in calls] == ["turn_on"]
    await tick(hass, freezer, 1)
    await task
    assert [c.service for c in calls] == ["turn_on", "turn_off"]
    assert suspends == [True, False]
    assert not manager.running


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["on", "unavailable", "unknown"])
async def test_maintenance_skips_active_or_unavailable(hass, state):
    """An active heating valve needs no exercise; unavailable valves cannot move."""
    calls = setup_switch(hass, False)
    hass.states.async_set(common.ENT_SWITCH, state)
    manager = ValveMaintenanceManager(hass, common.ENT_SWITCH, 7, lambda _: None)
    await manager.async_run_cycle()
    assert calls == []


@pytest.mark.asyncio
async def test_maintenance_cancel_closes_before_return(hass, freezer):
    """Cancellation closes an opened valve and cancels the hold timer."""
    calls = setup_switch(hass, False)
    manager = ValveMaintenanceManager(hass, common.ENT_SWITCH, 7, lambda _: None)
    task = asyncio.create_task(manager.async_run_cycle())
    await flush()
    await manager.cancel()
    assert [c.service for c in calls] == ["turn_on", "turn_off"]
    await tick(hass, freezer, 3600)
    assert len(calls) == 2
    assert task.done()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scheduled,remove", [(False, False), (False, True), (True, True)]
)
async def test_run_valve_maintenance_service_on_demand(
    hass, freezer, scheduled, remove
):
    """Service honours the option and removal also cancels service-only holds."""
    calls = setup_switch(hass, False)
    assert await async_setup_component(
        hass,
        "climate",
        {
            "climate": {
                "platform": DOMAIN,
                "name": "test",
                "heater": common.ENT_SWITCH,
                "target_sensor": common.ENT_SENSOR,
                "valve_maintenance_open_minutes": 2,
                "valve_maintenance": scheduled,
                "initial_hvac_mode": HVACMode.OFF,
            }
        },
    )
    await flush()
    await hass.async_block_till_done()
    thermostat = hass.data["entity_components"]["climate"].get_entity("climate.test")
    calls.clear()
    task = asyncio.create_task(
        hass.services.async_call(
            DOMAIN,
            "run_valve_maintenance",
            {"entity_id": "climate.test"},
            blocking=True,
        )
    )
    await flush()
    assert [c.service for c in calls] == ["turn_on"]
    await tick(hass, freezer, 60)
    assert len(calls) == 1
    if remove:
        await thermostat.async_remove()
    else:
        await tick(hass, freezer, 60)
    await flush()
    assert [c.service for c in calls] == ["turn_on", "turn_off"]
    await tick(hass, freezer, 600)
    assert len(calls) == 2
    assert task.done()
    if not remove:
        await thermostat.async_remove()


@pytest.mark.parametrize("value", [0, 31, 1.5])
def test_maintenance_yaml_rejects_invalid_hold(value):
    """YAML uses integer 1-30 minute bounds."""
    with pytest.raises(vol.Invalid):
        PLATFORM_SCHEMA(
            {
                "platform": DOMAIN,
                "heater": common.ENT_SWITCH,
                "target_sensor": common.ENT_SENSOR,
                "valve_maintenance_open_minutes": value,
            }
        )
