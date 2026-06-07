"""Presence Manager for Dual Smart Thermostat.

Presence sensing is the conceptual inverse of opening detection. Where an
*open* window/door pauses the HVAC, an *absence* of presence switches the
thermostat to the ``away`` preset. The state-tracking logic (availability,
per-entity debounce timeouts and an HVAC-mode scope) mirrors
:class:`OpeningManager` so the two features behave consistently.
"""

from datetime import timedelta
import enum
from itertools import chain
import logging
from typing import List

from homeassistant.components.climate import HVACMode
from homeassistant.const import (
    ATTR_ENTITY_ID,
    STATE_HOME,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
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

        Otherwise returns ``True`` if *any* available sensor reports someone
        present (the room is considered occupied while a single sensor sees
        someone). This is the raw, instantaneous occupancy. The absence
        debounce (waiting for the whole room to stay empty before switching to
        away) is applied to this aggregate by the climate entity, using
        :attr:`absence_timeout_seconds`, rather than per sensor.
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

        return any(self._is_present_state(p) for p in available)

    def _aggregate_timeout_seconds(self, timeout_attr: str) -> float:
        """Combine the per-sensor ``timeout_attr`` values into a single wait.

        The debounce is applied to the *aggregate* occupancy rather than per
        sensor, so the configured per-sensor values are combined into a single,
        most-conservative (longest) wait. Returns ``0`` when none is configured.

        Handles both the YAML form (``timedelta``) and the config/options flow
        form (plain seconds as ``int``/``float``).
        """
        timeouts: List[float] = []
        for presence in self.presence:
            value = presence.get(timeout_attr)
            if value is None:
                continue
            if isinstance(value, timedelta):
                timeouts.append(value.total_seconds())
            elif isinstance(value, (int, float)):
                timeouts.append(float(value))
        return max(timeouts) if timeouts else 0.0

    @property
    def absence_timeout_seconds(self) -> float:
        """Seconds the room must stay empty before switching to away.

        ``0`` means the away switch is immediate once the room is empty.
        """
        return self._aggregate_timeout_seconds(ATTR_ABSENCE_TIMEOUT)

    @property
    def presence_timeout_seconds(self) -> float:
        """Seconds presence must persist before the prior preset is restored.

        ``0`` means the restore is immediate as soon as any sensor is present.
        """
        return self._aggregate_timeout_seconds(ATTR_PRESENCE_TIMEOUT)
