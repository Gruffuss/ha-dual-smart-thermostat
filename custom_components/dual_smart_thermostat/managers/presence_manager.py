"""Presence Manager for Dual Smart Thermostat.

Presence sensing is the conceptual inverse of opening detection. Where an
*open* window/door pauses the HVAC, an *absence* of presence switches the
thermostat to the ``away`` preset. The state-tracking logic (availability,
per-entity debounce timeouts and an HVAC-mode scope) mirrors
:class:`OpeningManager` so the two features behave consistently.
"""

import enum
from itertools import chain
import logging
from typing import List

from homeassistant.components.climate import HVACMode
from homeassistant.const import (
    ATTR_ENTITY_ID,
    STATE_HOME,
    STATE_NOT_HOME,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import condition
from homeassistant.helpers.typing import ConfigType

from ..const import (
    ATTR_ABSENCE_TIMEOUT,
    ATTR_PRESENCE_TIMEOUT,
    CONF_PRESENCE,
    CONF_PRESENCE_SCOPE,
    TIMED_PRESENCE_SCHEMA,
)

_LOGGER = logging.getLogger(__name__)


class PresenceHvacModeScope(enum.StrEnum):
    """Presence Scope Options"""

    _ignore_ = "member cls"
    cls = vars()
    for member in chain(list(HVACMode)):
        cls[member.name] = member.value

    ALL = "all"


class PresenceManager:
    """Presence Manager for Dual Smart Thermostat."""

    def __init__(self, hass: HomeAssistant, config: ConfigType) -> None:
        self.hass = hass

        presence = config.get(CONF_PRESENCE)
        # The scope may arrive as a single string (YAML's ``vol.Any`` form or a
        # flow that persisted the raw selector value) or a list. Normalize to a
        # list so membership checks never run against a bare string (which would
        # do substring matching and raise on a None hvac mode at startup).
        scope = config.get(CONF_PRESENCE_SCOPE)
        if isinstance(scope, str):
            scope = [scope]
        self.presence_scope: List[PresenceHvacModeScope] = scope or [
            PresenceHvacModeScope.ALL
        ]

        self.presence = self.conform_presence_list(presence) if presence else []
        self.presence_entities = (
            self.conform_presence_entities(self.presence) if presence else []
        )
        self._presence_curr_state = {k: None for k in self.presence_entities}

    @staticmethod
    def conform_presence_list(presence: list) -> list:
        """Return a list of presence configs from a list of entities."""
        return [
            (entry if isinstance(entry, dict) else {ATTR_ENTITY_ID: entry})
            for entry in presence
        ]

    @staticmethod
    def conform_presence_entities(presence: [TIMED_PRESENCE_SCHEMA]) -> list:  # type: ignore
        """Return a list of entities from a list of presence configs."""
        return [entry[ATTR_ENTITY_ID] for entry in presence]

    def _is_presence_available(self, presence: TIMED_PRESENCE_SCHEMA) -> bool:  # type: ignore
        """If the presence sensor is available."""
        presence_entity = presence[ATTR_ENTITY_ID]
        presence_entity_state = self.hass.states.get(presence_entity)

        if presence_entity_state is None:
            _LOGGER.debug("Presence sensor %s is not available.", presence)
            return False

        if presence_entity_state.state == STATE_UNAVAILABLE:
            _LOGGER.debug("Presence sensor %s is unavailable.", presence)
            return False

        if presence_entity_state.state == STATE_UNKNOWN:
            _LOGGER.debug("Presence sensor %s is unknown.", presence)
            return False

        return True

    def _has_timeout_mode(self, presence: TIMED_PRESENCE_SCHEMA, is_present: bool) -> bool:  # type: ignore
        """If the presence sensor has a timeout mode for the given state."""
        timeout_attr = ATTR_PRESENCE_TIMEOUT if is_present else ATTR_ABSENCE_TIMEOUT
        return timeout_attr in presence

    def _is_present_state(self, presence: TIMED_PRESENCE_SCHEMA) -> bool:  # type: ignore
        """If the presence sensor currently reports someone present."""
        if not self._is_presence_available(presence):
            return False

        presence_entity = presence[ATTR_ENTITY_ID]
        return self.hass.states.is_state(
            presence_entity, STATE_ON
        ) or self.hass.states.is_state(presence_entity, STATE_HOME)

    def is_presence_detected(
        self, hvac_mode_scope: PresenceHvacModeScope = PresenceHvacModeScope.ALL
    ) -> bool:
        """If presence is currently detected within the requested scope.

        Returns ``True`` (present) whenever presence cannot or should not force
        an away switch, so the default behaviour is always safe:

        * no presence sensors configured -> always present
        * the requested HVAC mode is outside the configured scope -> present
        * no configured sensor is currently available -> present (a sensor
          outage must never trigger the away preset)

        Otherwise returns ``True`` only if at least one available sensor reports
        someone present, applying the per-sensor debounce timeouts.
        """
        _LOGGER.debug("is_presence_detected")
        if not self.presence_entities:
            return True

        _LOGGER.debug("Checking presence: %s", self.presence_entities)
        _LOGGER.debug("hvac_mode_scope: %s", hvac_mode_scope)

        in_scope = (
            hvac_mode_scope == PresenceHvacModeScope.ALL
            or (
                self.presence_scope != [PresenceHvacModeScope.ALL]
                and hvac_mode_scope in self.presence_scope
            )
            or PresenceHvacModeScope.ALL in self.presence_scope
        )

        if not in_scope:
            return True

        available = [p for p in self.presence if self._is_presence_available(p)]
        if not available:
            # No reliable information; never force an away switch on an outage.
            return True

        return any(self._is_present(p) for p in available)

    def _is_present(self, presence: TIMED_PRESENCE_SCHEMA) -> bool:  # type: ignore
        """If the presence sensor reports present, honoring debounce timeouts."""
        presence_entity = presence[ATTR_ENTITY_ID]

        # the sensor is unavailable -> treat as present (safe default)
        if not self._is_presence_available(presence):
            _LOGGER.debug("Presence sensor %s is not available.", presence)
            self._presence_curr_state[presence_entity] = True
            return True

        is_present = self._is_present_state(presence)
        # check timeout
        if self._has_timeout_mode(presence, is_present):
            _LOGGER.debug(
                "Have timeout mode for presence: %s, is present: %s",
                presence,
                is_present,
            )

            result = is_present
            if self._is_presence_timed_out(presence, is_present):
                result = is_present

            # this is to avoid debounce when state changes multiple times
            # inside the timeout interval or incorrect detection at startup
            elif (
                self._presence_curr_state[presence_entity] == is_present
                or self._presence_curr_state[presence_entity] is None
            ):
                result = is_present

            else:
                result = not is_present

            self._presence_curr_state[presence_entity] = result
            return result

        _LOGGER.debug(
            "No timeout mode for presence %s, is present: %s.",
            presence,
            is_present,
        )
        self._presence_curr_state[presence_entity] = is_present
        return is_present

    def _is_presence_timed_out(self, presence: TIMED_PRESENCE_SCHEMA, check_present: True) -> bool:  # type: ignore
        presence_entity = presence[ATTR_ENTITY_ID]
        timeout_attr = ATTR_PRESENCE_TIMEOUT if check_present else ATTR_ABSENCE_TIMEOUT

        _LOGGER.debug(
            "Checking if presence %s is timed out, state: %s, timeout: %s, waiting state: %s",
            presence,
            self.hass.states.get(presence_entity),
            presence[timeout_attr],
            STATE_ON if check_present else STATE_OFF,
        )
        if condition.state(
            self.hass,
            presence_entity,
            STATE_ON if check_present else STATE_OFF,
            presence[timeout_attr],
        ) or condition.state(
            self.hass,
            presence_entity,
            STATE_HOME if check_present else STATE_NOT_HOME,
            presence[timeout_attr],
        ):
            return True
        return False
