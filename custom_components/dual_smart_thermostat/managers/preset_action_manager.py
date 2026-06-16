"""Applies and restores preset *actions* (fan mode + external switches)."""

from __future__ import annotations

import logging

from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant

from ..managers.feature_manager import FeatureManager
from ..preset_env.preset_env import PresetEnv

_LOGGER = logging.getLogger(__name__)

_VALID_SWITCH_STATES = ("on", "off")


class PresetActionManager:
    """Apply fan mode + switch actions for a preset and restore them on exit.

    The baseline (fan mode and switch states present *before* a preset's
    actions were applied) is captured on first apply and used by
    ``async_restore``. It can be serialized into the climate entity's
    extra_state_attributes so restore survives a Home Assistant restart.
    """

    def __init__(self, hass: HomeAssistant, features: FeatureManager) -> None:
        self.hass = hass
        self._features = features
        self._baseline: dict | None = None

    # ----- apply ---------------------------------------------------------------

    async def async_apply(self, preset_env: PresetEnv) -> None:
        """Apply a preset's fan mode + switches, capturing a baseline first."""
        if preset_env is None or not (
            preset_env.has_fan_mode() or preset_env.has_switches()
        ):
            return

        if self._baseline is None:
            self._capture_baseline(preset_env)

        await self._apply_fan_mode(preset_env)
        await self._apply_switches(preset_env)

    def _capture_baseline(self, preset_env: PresetEnv) -> None:
        # Captured only on the first apply (async_apply guards on
        # self._baseline is None). The climate entity restores the baseline
        # before applying a different preset, so consecutive presets never
        # clobber the original pre-preset state recorded here.
        baseline: dict = {"fan_mode": None, "switches": {}}

        if preset_env.has_fan_mode():
            fan_device = self._features.fan_device
            if fan_device is not None and self._features.supports_fan_mode:
                baseline["fan_mode"] = fan_device.current_fan_mode

        if preset_env.has_switches():
            for entity_id in preset_env.switches:
                state = self.hass.states.get(entity_id)
                if state is not None and state.state in _VALID_SWITCH_STATES:
                    baseline["switches"][entity_id] = state.state

        self._baseline = baseline

    async def _apply_fan_mode(self, preset_env: PresetEnv) -> None:
        if not preset_env.has_fan_mode():
            return
        if not self._features.supports_fan_mode:
            _LOGGER.warning(
                "Preset requests fan mode %s but device does not support fan "
                "speed control; skipping",
                preset_env.fan_mode,
            )
            return
        fan_device = self._features.fan_device
        if fan_device is None:
            _LOGGER.warning("Preset requests fan mode but no fan device found")
            return
        if not fan_device.fan_modes or preset_env.fan_mode not in fan_device.fan_modes:
            _LOGGER.warning(
                "Preset fan mode %s not supported by device (supported: %s); "
                "skipping",
                preset_env.fan_mode,
                fan_device.fan_modes,
            )
            return
        await fan_device.async_set_fan_mode(preset_env.fan_mode)

    async def _apply_switches(self, preset_env: PresetEnv) -> None:
        if not preset_env.has_switches():
            return
        for entity_id in preset_env.switches:
            state = self.hass.states.get(entity_id)
            if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                _LOGGER.warning("Preset switch %s unavailable; skipping", entity_id)
                continue
            await self.hass.services.async_call(
                "homeassistant",
                "turn_on",
                {ATTR_ENTITY_ID: entity_id},
                blocking=True,
            )

    # ----- baseline (de)serialization -----------------------------------------

    def serialize_baseline(self) -> dict | None:
        """Return the held baseline for persistence, or None if not set."""
        return self._baseline

    def restore_baseline(self, data: dict | None) -> None:
        """Re-inject a baseline restored from persisted state."""
        self._baseline = data or None
