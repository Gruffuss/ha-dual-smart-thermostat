"""Behavioral tests for periodic valve maintenance (anti-stick)."""

from homeassistant.components.climate import HVACMode
from homeassistant.const import SERVICE_TURN_OFF, SERVICE_TURN_ON
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util.unit_system import METRIC_SYSTEM
import pytest

from custom_components.dual_smart_thermostat.const import DOMAIN
from custom_components.dual_smart_thermostat.managers.valve_maintenance_manager import (
    ValveMaintenanceManager,
)
from tests import common, setup_switch


@pytest.mark.asyncio
async def test_maintenance_cycle_pulses_and_restores(hass: HomeAssistant) -> None:
    """A maintenance cycle pulses the switch on/off twice then restores off."""
    calls = setup_switch(hass, False)  # switch starts off

    suspends: list[bool] = []
    manager = ValveMaintenanceManager(
        hass,
        common.ENT_SWITCH,
        interval_days=7,
        suspend_control=suspends.append,
        segment_seconds=0,
    )

    await manager.async_run_cycle()
    await hass.async_block_till_done()

    services = [c.service for c in calls]
    # 2 cycles of on/off, then restore (off because it started off).
    assert services == [
        SERVICE_TURN_ON,
        SERVICE_TURN_OFF,
        SERVICE_TURN_ON,
        SERVICE_TURN_OFF,
        SERVICE_TURN_OFF,
    ]
    # Control was suspended for the duration, then resumed.
    assert suspends == [True, False]
    assert manager.running is False


@pytest.mark.asyncio
async def test_maintenance_restores_prior_on_state(hass: HomeAssistant) -> None:
    """If the switch was on, it is restored on after maintenance."""
    calls = setup_switch(hass, True)  # switch starts on

    manager = ValveMaintenanceManager(
        hass,
        common.ENT_SWITCH,
        interval_days=7,
        suspend_control=lambda _: None,
        segment_seconds=0,
    )

    await manager.async_run_cycle()
    await hass.async_block_till_done()

    assert calls[-1].service == SERVICE_TURN_ON  # restored on


@pytest.mark.asyncio
async def test_maintenance_skips_unavailable_switch(hass: HomeAssistant) -> None:
    """No actions when the switch entity is unavailable."""
    from homeassistant.const import STATE_UNAVAILABLE

    calls = setup_switch(hass, False)
    hass.states.async_set(common.ENT_SWITCH, STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    calls.clear()

    manager = ValveMaintenanceManager(
        hass,
        common.ENT_SWITCH,
        interval_days=7,
        suspend_control=lambda _: None,
        segment_seconds=0,
    )
    await manager.async_run_cycle()
    await hass.async_block_till_done()

    assert calls == []


@pytest.mark.asyncio
async def test_maintenance_enabled_creates_manager(hass: HomeAssistant) -> None:
    """Enabling valve maintenance wires up a manager on the entity."""
    setup_switch(hass, False)
    hass.config.units = METRIC_SYSTEM
    assert await async_setup_component(
        hass,
        "climate",
        {
            "climate": {
                "platform": DOMAIN,
                "name": "test",
                "heater": common.ENT_SWITCH,
                "target_sensor": common.ENT_SENSOR,
                "valve_maintenance": True,
                "valve_maintenance_interval": 5,
                "initial_hvac_mode": HVACMode.HEAT,
            }
        },
    )
    await hass.async_block_till_done()

    thermostat = hass.data["entity_components"]["climate"].get_entity("climate.test")
    assert thermostat._valve_maintenance_manager is not None


@pytest.mark.asyncio
async def test_run_valve_maintenance_service_on_demand(hass: HomeAssistant) -> None:
    """The run_valve_maintenance service pulses the valve even when disabled."""
    calls = setup_switch(hass, False)
    hass.config.units = METRIC_SYSTEM
    assert await async_setup_component(
        hass,
        "climate",
        {
            "climate": {
                "platform": DOMAIN,
                "name": "test",
                "heater": common.ENT_SWITCH,
                "target_sensor": common.ENT_SENSOR,
                # scheduled maintenance intentionally left disabled
                "initial_hvac_mode": HVACMode.HEAT,
            }
        },
    )
    await hass.async_block_till_done()

    thermostat = hass.data["entity_components"]["climate"].get_entity("climate.test")
    assert thermostat._valve_maintenance_manager is None  # not scheduled

    # Speed up the transient cycle for the test.
    import custom_components.dual_smart_thermostat.managers.valve_maintenance_manager as vm

    orig_init = vm.ValveMaintenanceManager.__init__

    def fast_init(self, hass_, entity_id, interval_days, suspend_control, **kwargs):
        orig_init(
            self,
            hass_,
            entity_id,
            interval_days,
            suspend_control,
            segment_seconds=0,
        )

    vm.ValveMaintenanceManager.__init__ = fast_init
    try:
        calls.clear()
        await hass.services.async_call(
            DOMAIN,
            "run_valve_maintenance",
            {"entity_id": "climate.test"},
            blocking=True,
        )
        await hass.async_block_till_done()
    finally:
        vm.ValveMaintenanceManager.__init__ = orig_init

    services = [c.service for c in calls]
    assert services == [
        SERVICE_TURN_ON,
        SERVICE_TURN_OFF,
        SERVICE_TURN_ON,
        SERVICE_TURN_OFF,
        SERVICE_TURN_OFF,
    ]
