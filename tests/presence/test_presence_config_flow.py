"""Config and options flow integration tests for presence sensing.

These mirror the openings flow tests: presence is selected in the features
step, then configured with a sensor list, scope and optional debounce timeouts.
"""

from unittest.mock import Mock

from homeassistant.const import CONF_NAME
from homeassistant.data_entry_flow import FlowResultType
import pytest

from custom_components.dual_smart_thermostat.config_flow import ConfigFlowHandler
from custom_components.dual_smart_thermostat.const import (
    CONF_COOLER,
    CONF_HEATER,
    CONF_PRESENCE,
    CONF_PRESENCE_SCOPE,
    CONF_SENSOR,
    CONF_SYSTEM_TYPE,
    DOMAIN,
    SYSTEM_TYPE_HEATER_COOLER,
)


@pytest.fixture
def mock_hass():
    """Create a mock Home Assistant instance."""
    hass = Mock()
    hass.config_entries = Mock()
    hass.config_entries.async_entries = Mock(return_value=[])
    hass.data = {DOMAIN: {}}
    return hass


async def _advance_to_features(flow: ConfigFlowHandler) -> None:
    """Walk a heater_cooler flow up to the features step."""
    await flow.async_step_user({CONF_SYSTEM_TYPE: SYSTEM_TYPE_HEATER_COOLER})
    await flow.async_step_heater_cooler(
        {
            CONF_NAME: "Test HVAC",
            CONF_SENSOR: "sensor.temperature",
            CONF_HEATER: "switch.heater",
            CONF_COOLER: "switch.cooler",
        }
    )


async def test_presence_single_sensor(mock_hass):
    """Presence with a single sensor is saved through the config flow."""
    flow = ConfigFlowHandler()
    flow.hass = mock_hass
    flow.collected_config = {}

    await _advance_to_features(flow)

    result = await flow.async_step_features(
        {
            "configure_floor_heating": False,
            "configure_fan": False,
            "configure_humidity": False,
            "configure_openings": False,
            "configure_presence": True,
            "configure_presets": False,
        }
    )

    assert result["step_id"] == "presence_selection"

    result = await flow.async_step_presence_selection(
        {"selected_presence": ["binary_sensor.living_room"]}
    )
    assert result["step_id"] == "presence_config"

    result = await flow.async_step_presence_config(
        {
            CONF_PRESENCE_SCOPE: "all",
            "presence_1_timeout_present": 0,
            "presence_1_timeout_absent": 0,
        }
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    presence = flow.collected_config[CONF_PRESENCE]
    assert presence == [{"entity_id": "binary_sensor.living_room"}]
    # "all" scope is the default and should not be persisted.
    assert CONF_PRESENCE_SCOPE not in flow.collected_config


async def test_presence_with_timeouts_and_scope(mock_hass):
    """Per-sensor timeouts and a non-default scope are persisted."""
    flow = ConfigFlowHandler()
    flow.hass = mock_hass
    flow.collected_config = {}

    await _advance_to_features(flow)
    await flow.async_step_features(
        {
            "configure_floor_heating": False,
            "configure_fan": False,
            "configure_humidity": False,
            "configure_openings": False,
            "configure_presence": True,
            "configure_presets": False,
        }
    )
    await flow.async_step_presence_selection(
        {"selected_presence": ["binary_sensor.a", "binary_sensor.b"]}
    )
    result = await flow.async_step_presence_config(
        {
            CONF_PRESENCE_SCOPE: "heat",
            "presence_1_timeout_present": 0,
            "presence_1_timeout_absent": 120,
            "presence_2_timeout_present": 30,
            "presence_2_timeout_absent": 0,
        }
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    presence = flow.collected_config[CONF_PRESENCE]
    assert presence == [
        {"entity_id": "binary_sensor.a", "absence_timeout": 120},
        {"entity_id": "binary_sensor.b", "timeout": 30},
    ]
    assert flow.collected_config[CONF_PRESENCE_SCOPE] == "heat"


async def test_presence_disabled_not_in_config(mock_hass):
    """When presence is not enabled the flow skips it entirely."""
    flow = ConfigFlowHandler()
    flow.hass = mock_hass
    flow.collected_config = {}

    await _advance_to_features(flow)
    result = await flow.async_step_features(
        {
            "configure_floor_heating": False,
            "configure_fan": False,
            "configure_humidity": False,
            "configure_openings": False,
            "configure_presence": False,
            "configure_presets": False,
        }
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert CONF_PRESENCE not in flow.collected_config
