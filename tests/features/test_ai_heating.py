"""Behavioral tests for AI Time Based (learning PWM) heating control."""

from homeassistant.components.climate import HVACMode
from homeassistant.const import SERVICE_TURN_ON
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util.unit_system import METRIC_SYSTEM
import pytest

from custom_components.dual_smart_thermostat.const import (
    ATTR_HEAT_LOSS,
    ATTR_HEATING_POWER,
    DOMAIN,
)
from custom_components.dual_smart_thermostat.hvac_device.ai_heater_device import (
    AiHeaterDevice,
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


async def _setup_ai_heater(
    hass: HomeAssistant,
    *,
    target_temp: float = 21,
    cycle_seconds: int = 600,
    initial_heating_power: float = 0.01,
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
                "heater_control_mode": "ai",
                "pwm_cycle_duration": cycle_seconds,
                "ai_initial_heating_power": initial_heating_power,
                "target_temp": target_temp,
                "initial_hvac_mode": HVACMode.HEAT,
            }
        },
    )
    await hass.async_block_till_done()


def _device(hass: HomeAssistant):
    return (
        hass.data["entity_components"]["climate"].get_entity("climate.test").hvac_device
    )


@pytest.mark.asyncio
async def test_ai_mode_creates_ai_device(hass: HomeAssistant) -> None:
    await _setup_ai_heater(hass)
    assert isinstance(_device(hass), AiHeaterDevice)


@pytest.mark.asyncio
async def test_ai_heats_when_below_target(hass: HomeAssistant) -> None:
    """AI computes a positive duty and turns the heater on when below target."""
    calls = setup_switch(hass, False)
    await _setup_ai_heater(hass, target_temp=21, initial_heating_power=0.01)

    setup_sensor(hass, 19.5)  # 1.5 °C below target
    await hass.async_block_till_done()

    assert [c for c in calls if c.service == SERVICE_TURN_ON], "should heat"
    device = _device(hass)
    assert device.duty_cycle is not None and device.duty_cycle > 0


@pytest.mark.asyncio
async def test_ai_at_target_no_heat(hass: HomeAssistant) -> None:
    calls = setup_switch(hass, False)
    await _setup_ai_heater(hass, target_temp=21)

    setup_sensor(hass, 21.5)  # above target
    await hass.async_block_till_done()

    assert not [c for c in calls if c.service == SERVICE_TURN_ON]
    assert _device(hass).duty_cycle == 0.0


@pytest.mark.asyncio
async def test_ai_exposes_learned_values_as_attributes(hass: HomeAssistant) -> None:
    setup_switch(hass, False)
    await _setup_ai_heater(hass, initial_heating_power=0.02)

    setup_sensor(hass, 19.0)
    await hass.async_block_till_done()

    state = hass.states.get("climate.test")
    assert state.attributes.get(ATTR_HEATING_POWER) == 0.02
    assert ATTR_HEAT_LOSS in state.attributes


@pytest.mark.asyncio
async def test_ai_reset_service_restores_defaults(hass: HomeAssistant) -> None:
    setup_switch(hass, False)
    await _setup_ai_heater(hass, initial_heating_power=0.05)

    setup_sensor(hass, 19.0)
    await hass.async_block_till_done()

    device = _device(hass)
    assert device.heating_power == 0.05

    await hass.services.async_call(
        DOMAIN,
        "reset_heating_power",
        {"entity_id": "climate.test"},
        blocking=True,
    )
    await hass.async_block_till_done()

    # Reset returns to the module default (0.01).
    assert device.heating_power == 0.01


@pytest.mark.asyncio
async def test_ai_restores_learned_state(hass: HomeAssistant) -> None:
    """Learned heating power is restored from persisted state on startup."""
    from homeassistant.core import State

    from tests.common import mock_restore_cache

    mock_restore_cache(
        hass,
        (
            State(
                "climate.test",
                HVACMode.HEAT,
                {ATTR_HEATING_POWER: 0.08, ATTR_HEAT_LOSS: 0.03},
            ),
        ),
    )

    setup_switch(hass, False)
    await _setup_ai_heater(hass, initial_heating_power=0.01)
    await hass.async_block_till_done()

    device = _device(hass)
    assert device.heating_power == 0.08
    assert device.heat_loss_rate == 0.03
