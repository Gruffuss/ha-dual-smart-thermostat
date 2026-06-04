import logging

from homeassistant.components.climate import HVACMode
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.exceptions import ConditionError
from homeassistant.helpers import condition

from ..hvac_controller.cooler_controller import CoolerHvacController

_LOGGER = logging.getLogger(__name__)


class WrappedClimateHvacController(CoolerHvacController):
    """Controller for a wrapped ``climate`` entity (e.g. a split AC).

    The generic controller decides whether the controlled device is running by
    checking ``state == STATE_ON``. A ``climate`` entity's state is its
    ``hvac_mode`` (e.g. ``"cool"`` / ``"off"``) and never equals ``"on"``, so
    this controller treats "active" as "the wrapped entity is in any mode other
    than off/unavailable".
    """

    @property
    def is_active(self) -> bool:
        """If the wrapped climate entity is currently running."""
        if self.entity_id is None:
            return False
        state = self.hass.states.get(self.entity_id)
        if state is None:
            return False
        return state.state not in (
            HVACMode.OFF,
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        )

    def ran_long_enough(self) -> bool:
        """Whether the wrapped entity held its current state long enough.

        Uses the wrapped entity's actual current state (its ``hvac_mode``)
        instead of ``STATE_ON`` so ``min_cycle_duration`` works for climate
        entities.
        """
        state = self.hass.states.get(self.entity_id) if self.entity_id else None
        if state is None:
            return False

        current_state = state.state

        _LOGGER.debug(
            "Checking if wrapped climate %s ran long enough in state %s",
            self.entity_id,
            current_state,
        )

        try:
            long_enough = condition.state(
                self.hass,
                self.entity_id,
                current_state,
                self.min_cycle_duration,
            )
        except ConditionError:
            long_enough = False

        return long_enough
