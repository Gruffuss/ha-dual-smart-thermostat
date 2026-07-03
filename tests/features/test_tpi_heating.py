"""Behavioral tests for TPI (PWM) heating control of a heater switch."""

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.climate import HVACMode
from homeassistant.const import SERVICE_TURN_OFF, SERVICE_TURN_ON
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util.unit_system import METRIC_SYSTEM
import pytest

from custom_components.dual_smart_thermostat.const import DOMAIN
from custom_components.dual_smart_thermostat.hvac_device.pwm_heater_device import (
    PwmHeaterDevice,
)
from tests import common, setup_sensor, setup_switch


@pytest.fixture
def expected_lingering_timers() -> bool:
    """These tests set up via YAML platform config, so there is no config
    entry to unload and the entity is never removed. The PWM/valve timers
    are cancelled on entity removal (async_on_remove), which teardown here
    never triggers - tell the harness the scheduled timer is expected.
    """
    return True


async def _setup_tpi_heater(
    hass: HomeAssistant,
    *,
    target_temp: float = 20,
    cycle_seconds: int = 600,
    coef_int: float = 0.6,
    coef_ext: float = 0.01,
) -> None:
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
                "heater_control_mode": "tpi",
                "pwm_cycle_duration": cycle_seconds,
                "tpi_coef_int": coef_int,
                "tpi_coef_ext": coef_ext,
                "target_temp": target_temp,
                "initial_hvac_mode": HVACMode.HEAT,
            }
        },
    )
    await hass.async_block_till_done()


def _get_heater_device(hass: HomeAssistant):
    thermostat = hass.data["entity_components"]["climate"].get_entity("climate.test")
    return thermostat.hvac_device


@pytest.mark.asyncio
async def test_tpi_mode_creates_pwm_device(hass: HomeAssistant) -> None:
    """TPI control mode builds a PWM heater device."""
    await _setup_tpi_heater(hass)
    assert isinstance(_get_heater_device(hass), PwmHeaterDevice)


@pytest.mark.asyncio
async def test_tpi_full_duty_turns_heater_on(hass: HomeAssistant) -> None:
    """A large error yields ~100% duty -> heater stays on."""
    calls = setup_switch(hass, False)
    await _setup_tpi_heater(hass, target_temp=30)

    setup_sensor(hass, 20)  # error 10 -> duty clamps to 100
    await hass.async_block_till_done()

    on_calls = [c for c in calls if c.service == SERVICE_TURN_ON]
    assert on_calls, "Expected the heater to be switched on at full duty"

    device = _get_heater_device(hass)
    assert device.duty_cycle == 100.0


@pytest.mark.asyncio
async def test_tpi_overshoot_keeps_heater_off(hass: HomeAssistant) -> None:
    """Room above target -> 0% duty -> heater never turns on."""
    calls = setup_switch(hass, False)
    await _setup_tpi_heater(hass, target_temp=20)

    setup_sensor(hass, 25)  # well above target -> overshoot cutoff
    await hass.async_block_till_done()

    assert not [c for c in calls if c.service == SERVICE_TURN_ON]
    assert _get_heater_device(hass).duty_cycle == 0.0


@pytest.mark.asyncio
async def test_tpi_partial_duty_cycles_switch(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A partial duty turns the switch on, then off after the on-segment."""
    calls = setup_switch(hass, False)
    # error 0.5 * coef_int 0.6 = 30% duty; no outdoor term.
    await _setup_tpi_heater(hass, target_temp=20, cycle_seconds=600, coef_ext=0.0)

    setup_sensor(hass, 19.5)
    await hass.async_block_till_done()

    assert _get_heater_device(hass).duty_cycle == pytest.approx(30.0)
    assert [c for c in calls if c.service == SERVICE_TURN_ON], "should turn on"

    calls.clear()
    # 30% of 600s = 180s on-segment; advance past it -> turn off.
    freezer.tick(timedelta(seconds=190))
    common.async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert [c for c in calls if c.service == SERVICE_TURN_OFF], "should turn off"

    calls.clear()
    # remaining 420s off-segment; advance past it -> new cycle turns on again.
    freezer.tick(timedelta(seconds=430))
    common.async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert [c for c in calls if c.service == SERVICE_TURN_ON], "next cycle turns on"


@pytest.mark.asyncio
async def test_tpi_off_mode_stops_cycling(hass: HomeAssistant) -> None:
    """Turning the thermostat off stops PWM and turns the heater off."""
    calls = setup_switch(hass, False)
    await _setup_tpi_heater(hass, target_temp=30)

    setup_sensor(hass, 20)
    await hass.async_block_till_done()

    calls.clear()
    await common.async_set_hvac_mode(hass, HVACMode.OFF)
    await hass.async_block_till_done()

    assert [c for c in calls if c.service == SERVICE_TURN_OFF], "should turn off"
    device = _get_heater_device(hass)
    assert device._pwm.running is False
