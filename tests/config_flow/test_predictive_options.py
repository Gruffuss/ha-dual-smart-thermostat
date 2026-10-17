"""Real form submissions for predictive options and optional outdoor inputs."""

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dual_smart_thermostat.const import DOMAIN
from custom_components.dual_smart_thermostat.options_flow import OptionsFlowHandler


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["bang_bang", "tpi", "ai", "predictive"])
async def test_options_can_clear_outdoor_except_in_predictive(hass, mode):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "name": "test",
            "system_type": "simple_heater",
            "heater": "switch.heater",
            "target_sensor": "sensor.room",
            "outside_sensor": "sensor.outdoor",
            "heater_control_mode": "predictive",
            "pwm_cycle_duration": 1800,
            "min_cycle_duration": {"minutes": 10},
        },
    )
    entry.add_to_hass(hass)
    options = OptionsFlowHandler(entry)
    options.hass = hass
    form = await options.async_step_init()
    # The frontend omits an optional selector when the user clears it.
    submitted = form["data_schema"](
        {"advanced_settings": {"heater_control_mode": mode}}
    )
    result = await options.async_step_init(submitted)
    if mode == "predictive":
        assert result["type"] == "form"
        assert result["errors"] == {"base": "predictive_requires_outside_sensor"}
        assert entry.data["outside_sensor"] == "sensor.outdoor"
    else:
        assert result["type"] == "create_entry"
        assert not result["data"].get("outside_sensor")
        assert not entry.data.get("outside_sensor")
