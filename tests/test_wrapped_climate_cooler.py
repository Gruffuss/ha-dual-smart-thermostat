"""Tests for wrapping a real ``climate`` entity as the cooler.

These cover the heater_cooler system type where the cooler slot is a full
``climate`` entity (e.g. a split AC) instead of an on/off switch. The thermostat
owns the cooling decision based on the room sensor and delegates execution to the
wrapped entity via ``climate.set_hvac_mode`` / ``climate.set_temperature``.
"""

import logging

from homeassistant.components import input_boolean
from homeassistant.components.climate import (
    ATTR_FAN_MODE,
    ATTR_FAN_MODES,
    ATTR_HVAC_MODES,
    ATTR_SWING_MODE,
    ATTR_SWING_MODES,
    DOMAIN as CLIMATE,
    SERVICE_SET_FAN_MODE,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_SWING_MODE,
    SERVICE_SET_TEMPERATURE,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    ATTR_TEMPERATURE,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.setup import async_setup_component
from homeassistant.util.unit_system import METRIC_SYSTEM
import pytest

from custom_components.dual_smart_thermostat.const import DOMAIN

from . import common, setup_sensor  # noqa: F401

_LOGGER = logging.getLogger(__name__)

WRAPPED_AC = "climate.test_ac"
HEATER_SWITCH = common.ENT_HEATER  # input_boolean.test

WRAPPED_ATTRS = {
    ATTR_SUPPORTED_FEATURES: (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.SWING_MODE
    ),
    ATTR_HVAC_MODES: [HVACMode.OFF, HVACMode.COOL, HVACMode.HEAT],
    ATTR_FAN_MODES: ["auto", "low", "high"],
    ATTR_SWING_MODES: ["off", "vertical"],
    "min_temp": 16,
    "max_temp": 30,
    ATTR_TEMPERATURE: 24,
}


def _set_ac_state(hass: HomeAssistant, hvac_mode: str = HVACMode.OFF) -> None:
    """Seed the fake climate entity state (must be done before thermostat setup).

    The device detects fan/swing/temperature capabilities from this state during
    its ``__init__``, so it has to exist before the climate platform is set up.
    """
    hass.states.async_set(WRAPPED_AC, hvac_mode, dict(WRAPPED_ATTRS))


def _register_ac_handlers(hass: HomeAssistant):
    """Register fake climate services that capture and apply commands.

    Must be called AFTER the thermostat's climate platform is set up: the
    platform registers real entity-services for the climate domain, and these
    fake handlers intentionally override them so calls to the (unregistered)
    ``climate.test_ac`` entity are captured and reflected back into its state
    instead of being dropped as "missing or not currently available".

    Returns a dict of captured service-call lists keyed by service name.
    """
    calls = {
        SERVICE_SET_HVAC_MODE: [],
        SERVICE_SET_TEMPERATURE: [],
        SERVICE_SET_FAN_MODE: [],
        SERVICE_SET_SWING_MODE: [],
    }

    def _update_attr(key, value) -> None:
        current = hass.states.get(WRAPPED_AC)
        attrs = dict(current.attributes)
        attrs[key] = value
        hass.states.async_set(WRAPPED_AC, current.state, attrs)

    @callback
    def handle_set_hvac_mode(call) -> None:
        calls[SERVICE_SET_HVAC_MODE].append(call)
        current = hass.states.get(WRAPPED_AC)
        hass.states.async_set(
            WRAPPED_AC, call.data["hvac_mode"], dict(current.attributes)
        )

    @callback
    def handle_set_temperature(call) -> None:
        calls[SERVICE_SET_TEMPERATURE].append(call)
        _update_attr(ATTR_TEMPERATURE, call.data[ATTR_TEMPERATURE])

    @callback
    def handle_set_fan_mode(call) -> None:
        calls[SERVICE_SET_FAN_MODE].append(call)
        _update_attr(ATTR_FAN_MODE, call.data[ATTR_FAN_MODE])

    @callback
    def handle_set_swing_mode(call) -> None:
        calls[SERVICE_SET_SWING_MODE].append(call)
        _update_attr(ATTR_SWING_MODE, call.data[ATTR_SWING_MODE])

    hass.services.async_register(CLIMATE, SERVICE_SET_HVAC_MODE, handle_set_hvac_mode)
    hass.services.async_register(
        CLIMATE, SERVICE_SET_TEMPERATURE, handle_set_temperature
    )
    hass.services.async_register(CLIMATE, SERVICE_SET_FAN_MODE, handle_set_fan_mode)
    hass.services.async_register(CLIMATE, SERVICE_SET_SWING_MODE, handle_set_swing_mode)

    return calls


async def _setup_full(hass: HomeAssistant, target_temp: float = 22):
    """Seed AC state, set up the thermostat, then register the fake AC services.

    Returns the captured-calls dict from :func:`_register_ac_handlers`.
    """
    _set_ac_state(hass)
    await _setup_thermostat(hass, target_temp=target_temp)
    return _register_ac_handlers(hass)


async def _setup_thermostat(hass: HomeAssistant, target_temp: float = 22) -> None:
    hass.config.units = METRIC_SYSTEM
    assert await async_setup_component(
        hass,
        input_boolean.DOMAIN,
        {"input_boolean": {"test": None}},
    )
    assert await async_setup_component(
        hass,
        CLIMATE,
        {
            "climate": {
                "platform": DOMAIN,
                "name": "test",
                "cold_tolerance": 0.3,
                "hot_tolerance": 0.3,
                "heater": HEATER_SWITCH,
                "cooler": WRAPPED_AC,
                "target_sensor": common.ENT_SENSOR,
                "initial_hvac_mode": HVACMode.COOL,
                "target_temp": target_temp,
            }
        },
    )
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_wrapped_climate_cooling_commands_hvac_mode(hass: HomeAssistant) -> None:
    """When cooling is needed the wrapped climate entity is told to cool."""
    calls = await _setup_full(hass, target_temp=22)

    # Room is hotter than target + tolerance -> needs cooling.
    setup_sensor(hass, 26)
    await hass.async_block_till_done()

    cool_calls = [
        c
        for c in calls[SERVICE_SET_HVAC_MODE]
        if c.data.get(ATTR_ENTITY_ID) == WRAPPED_AC
        and c.data.get("hvac_mode") == HVACMode.COOL
    ]
    assert cool_calls, "Expected climate.set_hvac_mode(cool) on the wrapped AC"
    assert hass.states.get(WRAPPED_AC).state == HVACMode.COOL


@pytest.mark.asyncio
async def test_wrapped_climate_passes_target_temperature(hass: HomeAssistant) -> None:
    """The thermostat's target temperature is passed through to the wrapped AC."""
    calls = await _setup_full(hass, target_temp=21)

    setup_sensor(hass, 26)
    await hass.async_block_till_done()

    temp_calls = [
        c
        for c in calls[SERVICE_SET_TEMPERATURE]
        if c.data.get(ATTR_ENTITY_ID) == WRAPPED_AC
    ]
    assert temp_calls, "Expected climate.set_temperature on the wrapped AC"
    assert temp_calls[-1].data[ATTR_TEMPERATURE] == 21


@pytest.mark.asyncio
async def test_wrapped_climate_turns_off_when_goal_reached(
    hass: HomeAssistant,
) -> None:
    """When the room cools below target the wrapped AC is turned off."""
    calls = await _setup_full(hass, target_temp=22)

    # First make it cool.
    setup_sensor(hass, 26)
    await hass.async_block_till_done()
    assert hass.states.get(WRAPPED_AC).state == HVACMode.COOL

    # Now the room is below target - tolerance -> goal reached -> turn off.
    setup_sensor(hass, 20)
    await hass.async_block_till_done()

    off_calls = [
        c
        for c in calls[SERVICE_SET_HVAC_MODE]
        if c.data.get(ATTR_ENTITY_ID) == WRAPPED_AC
        and c.data.get("hvac_mode") == HVACMode.OFF
    ]
    assert off_calls, "Expected climate.set_hvac_mode(off) on the wrapped AC"
    assert hass.states.get(WRAPPED_AC).state == HVACMode.OFF


@pytest.mark.asyncio
async def test_wrapped_climate_off_mode_turns_off_ac(hass: HomeAssistant) -> None:
    """Setting the thermostat to OFF turns the wrapped AC off.

    The thermostat is driven through its entity object rather than the
    ``climate.set_hvac_mode`` service, because the fake AC handler in this test
    intercepts that service for the (shared) climate domain.
    """
    await _setup_full(hass, target_temp=22)

    setup_sensor(hass, 26)
    await hass.async_block_till_done()
    assert hass.states.get(WRAPPED_AC).state == HVACMode.COOL

    thermostat = hass.data["entity_components"][CLIMATE].get_entity("climate.test")
    await thermostat.async_set_hvac_mode(HVACMode.OFF)
    await hass.async_block_till_done()

    assert hass.states.get(WRAPPED_AC).state == HVACMode.OFF


@pytest.mark.asyncio
async def test_wrapped_climate_exposes_fan_and_swing(hass: HomeAssistant) -> None:
    """The thermostat surfaces the wrapped AC's fan and swing capabilities."""
    await _setup_full(hass, target_temp=22)

    state = hass.states.get("climate.test")
    assert state.attributes.get(ATTR_FAN_MODES) == ["auto", "low", "high"]
    assert state.attributes.get(ATTR_SWING_MODES) == ["off", "vertical"]

    features = state.attributes.get(ATTR_SUPPORTED_FEATURES)
    assert features & ClimateEntityFeature.FAN_MODE
    assert features & ClimateEntityFeature.SWING_MODE


@pytest.mark.asyncio
async def test_wrapped_climate_fan_mode_passthrough(hass: HomeAssistant) -> None:
    """Setting a fan mode is passed through to the wrapped AC."""
    calls = await _setup_full(hass, target_temp=22)

    thermostat = hass.data["entity_components"][CLIMATE].get_entity("climate.test")
    await thermostat.async_set_fan_mode("high")
    await hass.async_block_till_done()

    fan_calls = [
        c
        for c in calls[SERVICE_SET_FAN_MODE]
        if c.data.get(ATTR_ENTITY_ID) == WRAPPED_AC
    ]
    assert fan_calls, "Expected climate.set_fan_mode on the wrapped AC"
    assert fan_calls[-1].data[ATTR_FAN_MODE] == "high"
    assert hass.states.get("climate.test").attributes.get(ATTR_FAN_MODE) == "high"


@pytest.mark.asyncio
async def test_wrapped_climate_swing_mode_passthrough(hass: HomeAssistant) -> None:
    """Setting a swing mode is passed through to the wrapped AC."""
    calls = await _setup_full(hass, target_temp=22)

    thermostat = hass.data["entity_components"][CLIMATE].get_entity("climate.test")
    await thermostat.async_set_swing_mode("vertical")
    await hass.async_block_till_done()

    swing_calls = [
        c
        for c in calls[SERVICE_SET_SWING_MODE]
        if c.data.get(ATTR_ENTITY_ID) == WRAPPED_AC
    ]
    assert swing_calls, "Expected climate.set_swing_mode on the wrapped AC"
    assert swing_calls[-1].data[ATTR_SWING_MODE] == "vertical"
    assert hass.states.get("climate.test").attributes.get(ATTR_SWING_MODE) == "vertical"


@pytest.mark.asyncio
async def test_wrapped_climate_capabilities_detected(hass: HomeAssistant) -> None:
    """The wrapped entity's fan/swing capabilities are detected by the device."""
    from custom_components.dual_smart_thermostat.hvac_device.wrapped_climate_device import (  # noqa: E501
        WrappedClimateDevice,
    )

    _set_ac_state(hass)

    detected = {}

    original_detect = WrappedClimateDevice._detect_capabilities

    def spy_detect(self, state=None):
        original_detect(self, state)
        detected["fan"] = self.supports_fan_mode
        detected["fan_modes"] = self.fan_modes
        detected["swing"] = self.supports_swing_mode
        detected["swing_modes"] = self.swing_modes

    WrappedClimateDevice._detect_capabilities = spy_detect
    try:
        await _setup_thermostat(hass, target_temp=22)
    finally:
        WrappedClimateDevice._detect_capabilities = original_detect

    assert detected.get("fan") is True
    assert detected.get("fan_modes") == ["auto", "low", "high"]
    assert detected.get("swing") is True
    assert detected.get("swing_modes") == ["off", "vertical"]
