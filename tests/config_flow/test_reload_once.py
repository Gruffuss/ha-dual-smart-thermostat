"""Count actual climate platform setups after configuration changes."""

from unittest.mock import patch

from homeassistant.config_entries import SOURCE_RECONFIGURE
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dual_smart_thermostat import climate
from custom_components.dual_smart_thermostat.const import DOMAIN
from tests import common, setup_sensor, setup_switch


@pytest.mark.asyncio
@pytest.mark.parametrize("flow_type", ["reconfigure", "options"])
async def test_configuration_change_reloads_platform_once(hass, flow_type):
    setup_switch(hass, False)
    setup_sensor(hass, 21)
    hass.states.async_set(common.ENT_COOLER, "off")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "name": "test",
            "system_type": "heater_cooler",
            "heater": common.ENT_SWITCH,
            "cooler": common.ENT_COOLER,
            "target_sensor": common.ENT_SENSOR,
            "heater_control_mode": "ai",
            "target_temp": 21,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    with patch.object(
        climate, "async_setup_entry", wraps=climate.async_setup_entry
    ) as setup:
        if flow_type == "reconfigure":
            manager = hass.config_entries.flow
            result = await manager.async_init(
                DOMAIN,
                context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
            )
            result = await manager.async_configure(
                result["flow_id"], {"system_type": "heater_cooler"}
            )
            assert result["step_id"] == "heater_cooler"
            result = await manager.async_configure(
                result["flow_id"],
                {
                    "name": "test",
                    "heater": common.ENT_SWITCH,
                    "cooler": common.ENT_COOLER,
                    "target_sensor": common.ENT_SENSOR,
                    "advanced_settings": {
                        "heater_control_mode": "ai",
                        "pwm_cycle_duration": 1800,
                    },
                },
            )
            assert result["step_id"] == "features"
            result = await manager.async_configure(result["flow_id"], {})
            assert result["type"] == "abort"
            assert result["reason"] == "reconfigure_successful"
            assert entry.data["pwm_cycle_duration"] == 1800
        else:
            manager = hass.config_entries.options
            result = await manager.async_init(entry.entry_id)
            result = await manager.async_configure(
                result["flow_id"], {"target_temp": 22}
            )
            assert result["type"] == "create_entry"
            assert entry.data["target_temp"] == entry.options["target_temp"] == 22
        await hass.async_block_till_done()
        assert setup.call_count == 1
