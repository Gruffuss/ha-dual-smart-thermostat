"""Presence configuration steps.

Mirrors :mod:`feature_steps.openings` but for presence sensing. The selection
and per-entity debounce timeout + HVAC-mode scope handling follow the same
pattern so both features behave and look consistent in the config/options
flows. Presence sensing switches the thermostat to the ``away`` preset when
nobody is present.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
import voluptuous as vol

from ..const import CONF_PRESENCE, CONF_PRESENCE_SCOPE
from ..flow_utils import PresenceProcessor
from ..schemas import get_presence_selection_schema, get_presence_toggle_schema

_LOGGER = logging.getLogger(__name__)


class PresenceSteps:
    """Handle presence configuration steps for both config and options flows."""

    def __init__(self):
        """Initialize presence steps handler."""
        pass

    async def _call_next_step(self, next_step_handler):
        """Call next_step_handler and await only if it returns an awaitable."""
        result = next_step_handler()
        if hasattr(result, "__await__"):
            return await result
        return result

    async def async_step_toggle(
        self,
        flow_instance,
        user_input: dict[str, Any] | None,
        collected_config: dict,
        next_step_handler,
    ) -> FlowResult:
        """Handle presence toggle configuration."""
        if user_input is not None:
            collected_config.update(user_input)
            return await next_step_handler()

        return flow_instance.async_show_form(
            step_id="presence_toggle",
            data_schema=get_presence_toggle_schema(),
        )

    async def async_step_selection(
        self,
        flow_instance,
        user_input: dict[str, Any] | None,
        collected_config: dict,
        next_step_handler,
    ) -> FlowResult:
        """Handle presence selection configuration."""
        if user_input is not None:
            collected_config.update(user_input)
            return await self.async_step_config(
                flow_instance, None, collected_config, next_step_handler
            )

        schema = get_presence_selection_schema(
            defaults=collected_config.get("selected_presence", [])
        )

        return flow_instance.async_show_form(
            step_id="presence_selection", data_schema=schema
        )

    async def async_step_config(
        self,
        flow_instance,
        user_input: dict[str, Any] | None,
        collected_config: dict,
        next_step_handler,
    ) -> FlowResult:
        """Handle presence timeout/scope configuration."""
        if user_input is not None:
            collected_config.update(user_input)

            selected_entities = collected_config.get("selected_presence", [])

            presence_list = PresenceProcessor.process_presence_config(
                user_input, selected_entities
            )

            if presence_list:
                collected_config[CONF_PRESENCE] = presence_list

            PresenceProcessor.clean_presence_scope(collected_config)

            return await self._call_next_step(next_step_handler)

        selected_entities = collected_config.get("selected_presence", [])

        # If no entities selected, skip timeout configuration
        if not selected_entities:
            return await self._call_next_step(next_step_handler)

        schema_dict = self._build_config_schema_dict(
            collected_config, selected_entities
        )

        return flow_instance.async_show_form(
            step_id="presence_config",
            data_schema=vol.Schema(schema_dict),
            description_placeholders={
                "selected_entities": "\n".join(
                    f"• {entity_id}" for entity_id in selected_entities
                )
            },
        )

    def _build_config_schema_dict(
        self, collected_config: dict, selected_entities: list[str]
    ) -> dict:
        """Build the scope selector and per-entity timeout fields."""
        schema_dict: dict[Any, Any] = {}

        scope_options = self._build_scope_options(collected_config)

        schema_dict[
            vol.Optional(
                CONF_PRESENCE_SCOPE,
                default=collected_config.get(CONF_PRESENCE_SCOPE, "all"),
            )
        ] = selector.SelectSelector(
            selector.SelectSelectorConfig(options=scope_options)
        )

        # Extract existing timeout values from current config if available
        current_presence = collected_config.get(CONF_PRESENCE, [])
        existing_timeouts = {}
        for presence in current_presence:
            if isinstance(presence, dict):
                entity_id = presence["entity_id"]
                if entity_id in selected_entities:
                    existing_timeouts[entity_id] = {
                        "present": presence.get("timeout", 0),
                        "absent": presence.get("absence_timeout", 0),
                    }

        for i, entity_id in enumerate(selected_entities):
            if "." in entity_id:
                display_name = entity_id.split(".", 1)[1].replace("_", " ").title()
            else:
                display_name = entity_id.replace("_", " ").title()

            label_key = f"presence_{i + 1}_label"
            schema_dict[vol.Optional(label_key, default=f"👤 {display_name}")] = (
                selector.TextSelector(
                    selector.TextSelectorConfig(
                        type=selector.TextSelectorType.TEXT,
                        multiline=False,
                    )
                )
            )

            present_key = f"presence_{i + 1}_timeout_present"
            absent_key = f"presence_{i + 1}_timeout_absent"

            default_present = existing_timeouts.get(entity_id, {}).get("present", 0)
            default_absent = existing_timeouts.get(entity_id, {}).get("absent", 0)

            schema_dict[vol.Optional(present_key, default=default_present)] = (
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=3600, step=1, mode=selector.NumberSelectorMode.BOX
                    )
                )
            )

            schema_dict[vol.Optional(absent_key, default=default_absent)] = (
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=3600, step=1, mode=selector.NumberSelectorMode.BOX
                    )
                )
            )

        return schema_dict

    def _build_scope_options(self, collected_config: dict) -> list[dict]:
        """Build HVAC scope options based on the configured system."""
        scope_options = [{"value": "all", "label": "All HVAC modes"}]

        has_cooling = (
            bool(collected_config.get("cooler"))
            or bool(collected_config.get("ac_mode"))
            or bool(collected_config.get("heat_pump_cooling"))
        )
        if has_cooling:
            scope_options.append({"value": "cool", "label": "Cooling only"})

        has_heating = (
            bool(collected_config.get("heater"))
            and not (
                collected_config.get("ac_mode") and not collected_config.get("cooler")
            )
        ) or bool(collected_config.get("heat_pump_cooling"))

        if has_heating:
            scope_options.append({"value": "heat", "label": "Heating only"})

        if has_heating and has_cooling and collected_config.get("heat_cool_mode"):
            scope_options.append({"value": "heat_cool", "label": "Heat/Cool mode"})

        if collected_config.get("fan") or collected_config.get("fan_mode"):
            scope_options.append({"value": "fan_only", "label": "Fan only"})

        if collected_config.get("dryer"):
            scope_options.append({"value": "dry", "label": "Dry mode"})

        return scope_options

    async def async_step_options(
        self,
        flow_instance,
        user_input: dict[str, Any] | None,
        collected_config: dict,
        next_step_handler,
        current_config: dict,
    ) -> FlowResult:
        """Handle presence options (for options flow)."""
        if user_input is not None:
            # First step: a selection-only submission renders the detailed form.
            if user_input.get("selected_presence") and not any(
                k
                for k in user_input.keys()
                if k == CONF_PRESENCE_SCOPE
                or k.endswith("_timeout_present")
                or k.endswith("_timeout_absent")
            ):
                collected_config["selected_presence"] = user_input["selected_presence"]
                return await self.async_step_config(
                    flow_instance, None, collected_config, next_step_handler
                )

            if user_input.get("selected_presence"):
                selected = user_input["selected_presence"]
            else:
                selected = collected_config.get("selected_presence", [])

            presence_list = PresenceProcessor.process_presence_config(
                user_input, selected
            )

            if presence_list:
                collected_config[CONF_PRESENCE] = presence_list

            if user_input.get(CONF_PRESENCE_SCOPE) is not None:
                collected_config[CONF_PRESENCE_SCOPE] = user_input[CONF_PRESENCE_SCOPE]

            if not selected:
                collected_config.pop(CONF_PRESENCE, None)
                collected_config.pop(CONF_PRESENCE_SCOPE, None)

            collected_config.update(user_input)
            return await self._call_next_step(next_step_handler)

        current_presence = current_config.get(CONF_PRESENCE, [])
        selected_entities = PresenceProcessor.extract_selected_entities_from_config(
            current_presence
        )

        schema = get_presence_selection_schema(defaults=selected_entities)

        return flow_instance.async_show_form(
            step_id="presence_options", data_schema=schema
        )
