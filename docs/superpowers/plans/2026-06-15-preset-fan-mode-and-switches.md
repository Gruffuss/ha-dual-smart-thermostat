# Preset Fan Mode + Feature Switches Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let any thermostat preset optionally set a fan mode on the wrapped AC climate entity and turn on a list of external switches while active, restoring both on exit.

**Architecture:** Extend `PresetEnv` with two optional fields (`fan_mode`, `switches`). A new `PresetActionManager` applies them and remembers the pre-preset baseline for restore; the climate entity calls it from `async_set_preset_mode` and persists the baseline in `extra_state_attributes` for restart safety. Config/options/reconfigure flows surface the two new per-preset fields.

**Tech Stack:** Python 3.13, Home Assistant 2025.1.0+, voluptuous + HA selectors, pytest / pytest-homeassistant-custom-component. Tests/lint run via `./scripts/docker-test` and `./scripts/docker-lint`.

---

## File structure

- **Modify** `custom_components/dual_smart_thermostat/preset_env/preset_env.py` — add `fan_mode` + `switches` fields and `has_fan_mode()` / `has_switches()` helpers.
- **Create** `custom_components/dual_smart_thermostat/managers/preset_action_manager.py` — apply/restore actions + baseline.
- **Modify** `custom_components/dual_smart_thermostat/const.py` — add `ATTR_PRESET_ACTION_BASELINE`, `CONF_PRESET_SWITCHES`.
- **Modify** `custom_components/dual_smart_thermostat/climate.py` — construct manager, call apply/restore, persist/restore baseline.
- **Modify** `custom_components/dual_smart_thermostat/schemas.py` — render `{preset}_fan_mode` + `{preset}_switches` fields.
- **Modify** `custom_components/dual_smart_thermostat/feature_steps/presets.py` — extend flatten/transform suffix maps.
- **Modify** `custom_components/dual_smart_thermostat/translations/en.json` — add `{preset}_switches` labels/descriptions.
- **Create** `tests/presets/test_preset_action_manager.py` — unit tests for the manager.
- **Create** `tests/presets/test_preset_actions_integration.py` — climate-level apply/restore tests.
- **Modify** `tests/config_flow/test_options_flow.py` — persistence of the new fields.

Conventions to follow: `homeassistant.turn_on` / `homeassistant.turn_off` services are used for switches (works for both `switch` and `input_boolean`). Fan mode is applied via `feature_manager.fan_device` (the same accessor `climate.async_set_fan_mode` uses), which is the `WrappedClimateDevice` when wrapping a climate AC.

---

### Task 1: Extend `PresetEnv` with fan_mode + switches

**Files:**
- Modify: `custom_components/dual_smart_thermostat/preset_env/preset_env.py`
- Modify: `custom_components/dual_smart_thermostat/const.py`
- Test: `tests/presets/test_preset_action_manager.py` (new file, first test here)

- [ ] **Step 1: Add the constant**

In `const.py`, directly after the `PRESET_ANTI_FREEZE = "Anti Freeze"` line (currently line 178), add:

```python
# Per-preset action fields (stored inside each preset's nested config dict).
CONF_PRESET_SWITCHES = "switches"
# extra_state_attributes key holding the pre-preset baseline for restore.
ATTR_PRESET_ACTION_BASELINE = "preset_action_baseline"
```

- [ ] **Step 2: Write the failing test**

Create `tests/presets/test_preset_action_manager.py`:

```python
"""Unit tests for preset action (fan mode + switches) support."""

from custom_components.dual_smart_thermostat.preset_env.preset_env import PresetEnv


def test_preset_env_parses_fan_mode_and_switches():
    env = PresetEnv(
        temperature=18,
        fan_mode="quiet",
        switches=["switch.office_ac_sleep_2"],
    )
    assert env.fan_mode == "quiet"
    assert env.switches == ["switch.office_ac_sleep_2"]
    assert env.has_fan_mode() is True
    assert env.has_switches() is True


def test_preset_env_without_actions_defaults_none():
    env = PresetEnv(temperature=20)
    assert env.fan_mode is None
    assert env.switches is None
    assert env.has_fan_mode() is False
    assert env.has_switches() is False
```

- [ ] **Step 3: Run test to verify it fails**

Run: `./scripts/docker-test tests/presets/test_preset_action_manager.py -v`
Expected: FAIL — `AttributeError: 'PresetEnv' object has no attribute 'fan_mode'`.

- [ ] **Step 4: Implement the fields**

In `preset_env.py`, add the import near the top (with the other `..const` import on line 14):

```python
from ..const import CONF_MAX_FLOOR_TEMP, CONF_MIN_FLOOR_TEMP, CONF_PRESET_SWITCHES
```

In `PresetEnv.__init__`, after the existing `_process_field(...)` calls (currently ending at line 80), add:

```python
        # Action fields: applied to the wrapped AC when the preset activates.
        self.fan_mode = kwargs.get("fan_mode") or None
        self.switches = kwargs.get(CONF_PRESET_SWITCHES) or None
```

Add these helper methods alongside `has_humidity()` (near line 201):

```python
    def has_fan_mode(self) -> bool:
        return self.fan_mode is not None

    def has_switches(self) -> bool:
        return bool(self.switches)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `./scripts/docker-test tests/presets/test_preset_action_manager.py -v`
Expected: PASS (both tests).

- [ ] **Step 6: Commit**

```bash
git add custom_components/dual_smart_thermostat/preset_env/preset_env.py custom_components/dual_smart_thermostat/const.py tests/presets/test_preset_action_manager.py
git commit -m "feat: add fan_mode and switches fields to PresetEnv"
```

---

### Task 2: Create `PresetActionManager` (apply + capture baseline)

**Files:**
- Create: `custom_components/dual_smart_thermostat/managers/preset_action_manager.py`
- Test: `tests/presets/test_preset_action_manager.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/presets/test_preset_action_manager.py`:

```python
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.dual_smart_thermostat.managers.preset_action_manager import (
    PresetActionManager,
)


def _make_manager(fan_device=None, supports_fan_mode=False, states=None):
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    state_map = states or {}
    hass.states.get = lambda eid: state_map.get(eid)

    features = MagicMock()
    features.fan_device = fan_device
    features.supports_fan_mode = supports_fan_mode

    return hass, features, PresetActionManager(hass, features)


def _state(value):
    s = MagicMock()
    s.state = value
    return s


@pytest.mark.asyncio
async def test_apply_sets_fan_mode_and_turns_on_switches():
    fan_device = MagicMock()
    fan_device.fan_modes = ["auto", "low", "quiet"]
    fan_device.current_fan_mode = "low"
    fan_device.async_set_fan_mode = AsyncMock()

    hass, features, mgr = _make_manager(
        fan_device=fan_device,
        supports_fan_mode=True,
        states={"switch.sleep": _state("off")},
    )
    env = PresetEnv(temperature=18, fan_mode="quiet", switches=["switch.sleep"])

    await mgr.async_apply(env)

    fan_device.async_set_fan_mode.assert_awaited_once_with("quiet")
    hass.services.async_call.assert_awaited_once_with(
        "homeassistant", "turn_on", {"entity_id": "switch.sleep"}, blocking=True
    )
    # Baseline captured the prior state for later restore.
    assert mgr.serialize_baseline() == {
        "fan_mode": "low",
        "switches": {"switch.sleep": "off"},
    }


@pytest.mark.asyncio
async def test_apply_skips_unsupported_fan_mode_value():
    fan_device = MagicMock()
    fan_device.fan_modes = ["auto", "low"]
    fan_device.current_fan_mode = "low"
    fan_device.async_set_fan_mode = AsyncMock()

    _, _, mgr = _make_manager(fan_device=fan_device, supports_fan_mode=True)
    env = PresetEnv(temperature=18, fan_mode="quiet")  # "quiet" not in fan_modes

    await mgr.async_apply(env)

    fan_device.async_set_fan_mode.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_noop_when_no_actions():
    hass, _, mgr = _make_manager()
    await mgr.async_apply(PresetEnv(temperature=20))
    hass.services.async_call.assert_not_awaited()
    assert mgr.serialize_baseline() is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./scripts/docker-test tests/presets/test_preset_action_manager.py -v`
Expected: FAIL — `ModuleNotFoundError: ... preset_action_manager`.

- [ ] **Step 3: Implement the manager (apply + baseline)**

Create `custom_components/dual_smart_thermostat/managers/preset_action_manager.py`:

```python
"""Applies and restores preset *actions* (fan mode + external switches)."""

from __future__ import annotations

import logging

from homeassistant.const import (
    ATTR_ENTITY_ID,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
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
        if fan_device.fan_modes and preset_env.fan_mode not in fan_device.fan_modes:
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
                _LOGGER.warning(
                    "Preset switch %s unavailable; skipping", entity_id
                )
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./scripts/docker-test tests/presets/test_preset_action_manager.py -v`
Expected: PASS (all 5 tests).

- [ ] **Step 5: Commit**

```bash
git add custom_components/dual_smart_thermostat/managers/preset_action_manager.py tests/presets/test_preset_action_manager.py
git commit -m "feat: add PresetActionManager apply + baseline capture"
```

---

### Task 3: `PresetActionManager.async_restore`

**Files:**
- Modify: `custom_components/dual_smart_thermostat/managers/preset_action_manager.py`
- Test: `tests/presets/test_preset_action_manager.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/presets/test_preset_action_manager.py`:

```python
@pytest.mark.asyncio
async def test_restore_reverts_fan_mode_and_switches():
    fan_device = MagicMock()
    fan_device.fan_modes = ["auto", "low", "quiet"]
    fan_device.current_fan_mode = "low"
    fan_device.async_set_fan_mode = AsyncMock()

    hass, _, mgr = _make_manager(
        fan_device=fan_device,
        supports_fan_mode=True,
        states={"switch.sleep": _state("off")},
    )
    env = PresetEnv(temperature=18, fan_mode="quiet", switches=["switch.sleep"])

    await mgr.async_apply(env)
    fan_device.async_set_fan_mode.reset_mock()
    hass.services.async_call.reset_mock()

    await mgr.async_restore()

    # Fan mode reverted to the captured "low".
    fan_device.async_set_fan_mode.assert_awaited_once_with("low")
    # Switch reverted to its prior "off" state via turn_off.
    hass.services.async_call.assert_awaited_once_with(
        "homeassistant", "turn_off", {"entity_id": "switch.sleep"}, blocking=True
    )
    assert mgr.serialize_baseline() is None


@pytest.mark.asyncio
async def test_restore_without_baseline_is_noop():
    hass, _, mgr = _make_manager()
    await mgr.async_restore()
    hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_restore_baseline_roundtrip():
    hass, _, mgr = _make_manager(states={"switch.sleep": _state("on")})
    mgr.restore_baseline({"fan_mode": None, "switches": {"switch.sleep": "off"}})
    await mgr.async_restore()
    hass.services.async_call.assert_awaited_once_with(
        "homeassistant", "turn_off", {"entity_id": "switch.sleep"}, blocking=True
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./scripts/docker-test tests/presets/test_preset_action_manager.py -v`
Expected: FAIL — `AttributeError: ... has no attribute 'async_restore'`.

- [ ] **Step 3: Implement restore**

In `preset_action_manager.py`, add this method after `_apply_switches` (before the baseline (de)serialization section):

```python
    # ----- restore -------------------------------------------------------------

    async def async_restore(self) -> None:
        """Restore fan mode + switch states captured before the preset."""
        if self._baseline is None:
            return

        fan_mode = self._baseline.get("fan_mode")
        if (
            fan_mode is not None
            and self._features.supports_fan_mode
            and self._features.fan_device is not None
        ):
            await self._features.fan_device.async_set_fan_mode(fan_mode)

        for entity_id, prior in self._baseline.get("switches", {}).items():
            state = self.hass.states.get(entity_id)
            if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                continue
            service = "turn_on" if prior == "on" else "turn_off"
            await self.hass.services.async_call(
                "homeassistant",
                service,
                {ATTR_ENTITY_ID: entity_id},
                blocking=True,
            )

        self._baseline = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./scripts/docker-test tests/presets/test_preset_action_manager.py -v`
Expected: PASS (all 8 tests).

- [ ] **Step 5: Commit**

```bash
git add custom_components/dual_smart_thermostat/managers/preset_action_manager.py tests/presets/test_preset_action_manager.py
git commit -m "feat: add PresetActionManager restore"
```

---

### Task 4: Wire the manager into the climate entity

**Files:**
- Modify: `custom_components/dual_smart_thermostat/climate.py`
- Test: `tests/presets/test_preset_actions_integration.py` (new)

- [ ] **Step 1: Write the failing integration test**

Create `tests/presets/test_preset_actions_integration.py`:

```python
"""Integration: preset activation drives fan mode + switches on the AC."""

from unittest.mock import AsyncMock

from homeassistant.components.climate.const import PRESET_SLEEP, PRESET_NONE
import pytest


@pytest.mark.asyncio
async def test_sleep_preset_sets_fan_and_switch_then_restores(hass):
    """Selecting Sleep sets fan=quiet + turns the sleep switch on; None reverts."""
    from custom_components.dual_smart_thermostat.managers.preset_action_manager import (
        PresetActionManager,
    )

    # Spy on apply/restore so we assert the climate entity calls them on the
    # correct transitions without needing a real wrapped AC entity.
    apply_calls = []
    restore_calls = []

    orig_apply = PresetActionManager.async_apply
    orig_restore = PresetActionManager.async_restore

    async def spy_apply(self, preset_env):
        apply_calls.append(preset_env)
        return await orig_apply(self, preset_env)

    async def spy_restore(self):
        restore_calls.append(True)
        return await orig_restore(self)

    PresetActionManager.async_apply = spy_apply
    PresetActionManager.async_restore = spy_restore
    try:
        # Build a minimal heater+sensor thermostat with a Sleep preset that
        # carries actions. Reuse the project's setup helper pattern.
        from tests.conftest import setup_comp_heat  # noqa: F401

        # The fixture-driven entity is created by the shared helpers; this test
        # asserts transition wiring via the spies.
        # (Implementation note: see sibling preset tests for the setup helper
        # used by this repo; mirror their thermostat construction.)
        pytest.skip(
            "Replace with repo's standard preset thermostat setup; assert "
            "apply called on enter, restore on exit."
        )
    finally:
        PresetActionManager.async_apply = orig_apply
        PresetActionManager.async_restore = orig_restore
```

> Implementation note for the engineer: this repo's preset tests construct the
> thermostat through shared helpers in `tests/conftest.py` and
> `tests/presets/`. Open an existing test in `tests/presets/` (e.g. a preset
> temperature test), copy its exact setup (config dict + `async_setup_component`
> + `hass.services.async_call("climate", "set_preset_mode", ...)`), and replace
> the `pytest.skip` body so the test:
> 1. activates `PRESET_SLEEP` and asserts `len(apply_calls) == 1`,
> 2. activates `PRESET_NONE` and asserts `len(restore_calls) == 1`.
> Keep the spy wrapping above.

- [ ] **Step 2: Run test to verify it fails (or skips pending setup)**

Run: `./scripts/docker-test tests/presets/test_preset_actions_integration.py -v`
Expected: SKIP initially; after filling in setup it must FAIL because the climate entity does not yet call apply/restore.

- [ ] **Step 3: Construct the manager in setup**

In `climate.py`, add the import near the other manager imports at the top of the file:

```python
from .managers.preset_action_manager import PresetActionManager
```

In the setup helper, immediately after `preset_manager = PresetManager(...)` (line 513) add:

```python
    preset_action_manager = PresetActionManager(hass, feature_manager)
```

Pass it into the constructor: in the `DualSmartThermostat(...)` call (around line 522-543), add `preset_action_manager,` right after `preset_manager,`.

- [ ] **Step 4: Accept + store it in `__init__`**

In `DualSmartThermostat.__init__`, add the parameter after `preset_manager: PresetManager,` (line 619):

```python
        preset_action_manager: PresetActionManager,
```

And after `self.presets = preset_manager` (line 637) add:

```python
        # preset action manager (fan mode + switches)
        self._preset_actions = preset_action_manager
```

- [ ] **Step 5: Add the transition helper + call it from `async_set_preset_mode`**

In `climate.py`, add this method just above `async_set_preset_mode` (line 2222):

```python
    async def _async_apply_preset_actions(
        self, old_preset_mode: str, preset_mode: str
    ) -> None:
        """Restore the previous preset's actions and apply the new one's."""
        leaving = old_preset_mode != PRESET_NONE and old_preset_mode != preset_mode
        if leaving:
            await self._preset_actions.async_restore()
        if preset_mode != PRESET_NONE:
            await self._preset_actions.async_apply(self.presets.preset_env)
```

In `async_set_preset_mode`, after the humidity block (after line 2260, before `await self._setup_template_listeners()` on line 2263) add:

```python
        await self._async_apply_preset_actions(old_preset_mode, preset_mode)
```

- [ ] **Step 6: Also wire auto-preset selection**

In `_check_auto_preset_selection` (line 2304), change the body so it applies actions. Replace:

```python
        matching_preset = self.presets.find_matching_preset()
        if matching_preset:
            _LOGGER.info(
                "Auto-selecting preset '%s' due to matching values", matching_preset
            )
            self.presets.set_preset_mode(matching_preset)
            self._attr_preset_mode = self.presets.preset_mode
```

with:

```python
        old_preset_mode = self.presets.preset_mode
        matching_preset = self.presets.find_matching_preset()
        if matching_preset:
            _LOGGER.info(
                "Auto-selecting preset '%s' due to matching values", matching_preset
            )
            self.presets.set_preset_mode(matching_preset)
            self._attr_preset_mode = self.presets.preset_mode
            await self._async_apply_preset_actions(old_preset_mode, matching_preset)
```

- [ ] **Step 7: Run the integration test (after filling in setup from Step 1 note)**

Run: `./scripts/docker-test tests/presets/test_preset_actions_integration.py -v`
Expected: PASS.

- [ ] **Step 8: Run the broader preset + heater suites to catch constructor regressions**

Run: `./scripts/docker-test tests/presets/ tests/test_heater_mode.py -q`
Expected: PASS (the new constructor parameter must not break existing setups).

- [ ] **Step 9: Commit**

```bash
git add custom_components/dual_smart_thermostat/climate.py tests/presets/test_preset_actions_integration.py
git commit -m "feat: apply/restore preset actions from climate entity"
```

---

### Task 5: Persist + restore baseline across restart

**Files:**
- Modify: `custom_components/dual_smart_thermostat/climate.py`
- Test: `tests/presets/test_preset_action_manager.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/presets/test_preset_action_manager.py`:

```python
@pytest.mark.asyncio
async def test_serialize_then_restore_baseline_survives_roundtrip():
    fan_device = MagicMock()
    fan_device.fan_modes = ["auto", "low", "quiet"]
    fan_device.current_fan_mode = "low"
    fan_device.async_set_fan_mode = AsyncMock()

    _, _, mgr = _make_manager(
        fan_device=fan_device,
        supports_fan_mode=True,
        states={"switch.sleep": _state("off")},
    )
    await mgr.async_apply(
        PresetEnv(temperature=18, fan_mode="quiet", switches=["switch.sleep"])
    )

    snapshot = mgr.serialize_baseline()

    # Simulate restart: fresh manager, re-inject snapshot.
    _, _, mgr2 = _make_manager(
        fan_device=fan_device,
        supports_fan_mode=True,
        states={"switch.sleep": _state("on")},
    )
    mgr2.restore_baseline(snapshot)
    assert mgr2.serialize_baseline() == snapshot
```

- [ ] **Step 2: Run test to verify it passes already (manager-level)**

Run: `./scripts/docker-test "tests/presets/test_preset_action_manager.py::test_serialize_then_restore_baseline_survives_roundtrip" -v`
Expected: PASS (this validates the manager contract used by climate persistence).

- [ ] **Step 3: Persist the baseline in `extra_state_attributes`**

In `climate.py`, add the import to the existing `from .const import (...)` block:

```python
    ATTR_PRESET_ACTION_BASELINE,
```

In `extra_state_attributes` (line 1347), before the final `_LOGGER.debug("Extra state attributes: %s", attributes)` (line 1406) add:

```python
        # Persist the pre-preset action baseline so fan mode / switch state can
        # be reverted even after a Home Assistant restart while a preset with
        # actions is active.
        action_baseline = self._preset_actions.serialize_baseline()
        if action_baseline is not None:
            attributes[ATTR_PRESET_ACTION_BASELINE] = action_baseline
```

- [ ] **Step 4: Restore the baseline on startup**

In `async_added_to_hass`, immediately after `await self.presets.apply_old_state(old_state)` (line 1078) add:

```python
            # Re-inject the persisted preset-action baseline so a later preset
            # exit reverts fan mode / switches correctly after a restart.
            self._preset_actions.restore_baseline(
                old_state.attributes.get(ATTR_PRESET_ACTION_BASELINE)
            )
```

- [ ] **Step 5: Run the manager + preset suites**

Run: `./scripts/docker-test tests/presets/ -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add custom_components/dual_smart_thermostat/climate.py tests/presets/test_preset_action_manager.py
git commit -m "feat: persist and restore preset action baseline across restart"
```

---

### Task 6: Render the new config fields in the presets schema

**Files:**
- Modify: `custom_components/dual_smart_thermostat/schemas.py`
- Test: covered by Task 9 (config-flow persistence)

- [ ] **Step 1: Add the fields in `get_presets_schema`**

In `schemas.py`, inside `get_presets_schema`, at the end of the `for preset in selected_presets:` loop body — immediately before `return vol.Schema(schema_dict)` is wrong (that's outside the loop); instead add the block at the end of the loop iteration, right after the `if heat_cool_enabled: ... else: ...` temperature block (after line 1402, still inside the `for` loop). Add:

```python
        # Optional fan mode applied to the wrapped AC while the preset is active.
        # Free text so any device-specific mode (e.g. "quiet") is accepted;
        # validity is checked against the live device when the preset applies.
        existing_fan_mode = user_input.get(f"{preset_key}_fan_mode", "")
        if not isinstance(existing_fan_mode, str):
            existing_fan_mode = str(existing_fan_mode)
        schema_dict[
            vol.Optional(f"{preset_key}_fan_mode", default=existing_fan_mode)
        ] = selector.TextSelector(
            selector.TextSelectorConfig(
                multiline=False,
                type=selector.TextSelectorType.TEXT,
            )
        )

        # Optional switches/input_booleans turned on while the preset is active.
        existing_switches = user_input.get(f"{preset_key}_switches", [])
        if not isinstance(existing_switches, list):
            existing_switches = []
        schema_dict[
            vol.Optional(f"{preset_key}_switches", default=existing_switches)
        ] = selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain=["switch", "input_boolean"],
                multiple=True,
            )
        )
```

- [ ] **Step 2: Verify the module imports cleanly**

Run: `./scripts/docker-shell python -c "from custom_components.dual_smart_thermostat import schemas; print('ok')"`
Expected: prints `ok` (no syntax/import error).

- [ ] **Step 3: Commit**

```bash
git add custom_components/dual_smart_thermostat/schemas.py
git commit -m "feat: render preset fan_mode + switches fields in schema"
```

---

### Task 7: Map the new fields in flatten/transform

**Files:**
- Modify: `custom_components/dual_smart_thermostat/feature_steps/presets.py`
- Test: `tests/config_flow/test_options_flow.py` (added in Task 9)

- [ ] **Step 1: Extend `_transform_preset_fields_to_new_format`**

In `presets.py`, in `_transform_preset_fields_to_new_format`, update the imports inside the method (currently lines 195-202) to also bring in the switches constant:

```python
        from ..const import (
            CONF_MAX_FLOOR_TEMP,
            CONF_MIN_FLOOR_TEMP,
            CONF_PRESET_SWITCHES,
        )
        from ..const import ATTR_FAN_MODE
```

Extend the `field_mappings` dict (currently lines 208-215) with two entries:

```python
        field_mappings = {
            "_temp": ATTR_TEMPERATURE,
            "_temp_low": ATTR_TARGET_TEMP_LOW,
            "_temp_high": ATTR_TARGET_TEMP_HIGH,
            "_min_floor_temp": CONF_MIN_FLOOR_TEMP,
            "_max_floor_temp": CONF_MAX_FLOOR_TEMP,
            "_humidity": ATTR_HUMIDITY,
            "_fan_mode": ATTR_FAN_MODE,
            "_switches": CONF_PRESET_SWITCHES,
        }
```

Then make empty values drop out so a blank fan-mode text field or empty switch
list is not stored. Replace the matched-suffix block (currently lines 219-228):

```python
            for suffix, attr_name in field_mappings.items():
                if key.endswith(suffix):
                    # Extract preset key by removing the suffix
                    preset_key = key[: -len(suffix)]
                    if preset_key not in preset_data:
                        preset_data[preset_key] = {}
                    preset_data[preset_key][attr_name] = value
                    matched = True
                    break
```

with:

```python
            for suffix, attr_name in field_mappings.items():
                if key.endswith(suffix):
                    matched = True
                    # Drop empty values (blank fan mode, empty switch list) so
                    # they are not persisted as meaningless preset config.
                    if value in (None, "", []):
                        break
                    preset_key = key[: -len(suffix)]
                    if preset_key not in preset_data:
                        preset_data[preset_key] = {}
                    preset_data[preset_key][attr_name] = value
                    break
```

> Note: `"_temp"` is checked before `"_fan_mode"`/`"_switches"` in the dict, but
> no preset field name ends in both suffixes, so ordering is safe. `endswith`
> matches the longest meaningful suffix per field uniquely.

- [ ] **Step 2: Extend `_flatten_presets_for_form`**

In `_flatten_presets_for_form`, update the in-method imports (lines 149-156) to add the switches constant and fan-mode attr:

```python
        from homeassistant.components.climate.const import (
            ATTR_HUMIDITY,
            ATTR_TARGET_TEMP_HIGH,
            ATTR_TARGET_TEMP_LOW,
        )
        from homeassistant.const import ATTR_TEMPERATURE

        from ..const import (
            ATTR_FAN_MODE,
            CONF_MAX_FLOOR_TEMP,
            CONF_MIN_FLOOR_TEMP,
            CONF_PRESET_SWITCHES,
            CONF_PRESETS,
        )
```

Extend `attr_to_suffix` (lines 161-168):

```python
        attr_to_suffix = {
            ATTR_TEMPERATURE: "_temp",
            ATTR_TARGET_TEMP_LOW: "_temp_low",
            ATTR_TARGET_TEMP_HIGH: "_temp_high",
            CONF_MIN_FLOOR_TEMP: "_min_floor_temp",
            CONF_MAX_FLOOR_TEMP: "_max_floor_temp",
            ATTR_HUMIDITY: "_humidity",
            ATTR_FAN_MODE: "_fan_mode",
            CONF_PRESET_SWITCHES: "_switches",
        }
```

- [ ] **Step 3: Verify import + a quick transform round-trip**

Run:
```bash
./scripts/docker-shell python -c "
from custom_components.dual_smart_thermostat.feature_steps.presets import PresetsSteps
s = PresetsSteps()
out = s._transform_preset_fields_to_new_format({'sleep_temp': 18, 'sleep_fan_mode': 'quiet', 'sleep_switches': ['switch.sleep'], 'comfort_fan_mode': ''})
print(out)
assert out['sleep']['fan_mode'] == 'quiet'
assert out['sleep']['switches'] == ['switch.sleep']
assert 'comfort' not in out  # empty fan mode dropped
print('ok')
"
```
Expected: prints the dict then `ok`.

- [ ] **Step 4: Commit**

```bash
git add custom_components/dual_smart_thermostat/feature_steps/presets.py
git commit -m "feat: map preset fan_mode + switches fields in config flow"
```

---

### Task 8: Translations for the switches field

**Files:**
- Modify: `custom_components/dual_smart_thermostat/translations/en.json`

- [ ] **Step 1: Add `{preset}_switches` labels + descriptions**

For each preset that already has `{preset}_fan_mode` entries in the `presets` step's
`data` and `data_description` blocks (search the file for `"sleep_fan_mode"` to find
the location — there is one in the `config` flow block and one in the `options` flow
block), add a sibling `{preset}_switches` key. For Sleep, alongside
`"sleep_fan_mode": "Sleep fan mode"` add:

```json
                    "sleep_switches": "Sleep AC feature switches",
```

and alongside the `sleep_fan_mode` description add to `data_description`:

```json
                    "sleep_switches": "Switches or input_booleans turned on while the Sleep preset is active (e.g. the AC's native sleep switch). Restored to their previous state when the preset is cleared.",
```

Repeat for every other preset key present in that block (`away`, `home`, `comfort`,
`eco`, `anti_freeze`, `activity`, `boost`) using the same wording with the preset
name substituted. Do this in **both** the config-flow and options-flow translation
blocks where `*_fan_mode` already appears.

- [ ] **Step 2: Validate JSON**

Run: `./scripts/docker-shell python -c "import json; json.load(open('custom_components/dual_smart_thermostat/translations/en.json')); print('ok')"`
Expected: prints `ok`.

- [ ] **Step 3: Run the translations test**

Run: `./scripts/docker-test tests/config_flow/test_translations.py -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add custom_components/dual_smart_thermostat/translations/en.json
git commit -m "feat: add translations for preset switches field"
```

---

### Task 9: Config→options persistence test for the new fields

**Files:**
- Modify: `tests/config_flow/test_options_flow.py`

- [ ] **Step 1: Write the failing persistence test**

Open `tests/config_flow/test_options_flow.py`, find an existing options-flow preset
test (search for `presets` and `set_preset` patterns) to copy the exact flow-driving
setup this repo uses. Add a new test that mirrors that setup and asserts the new
fields survive the transform and land in the nested preset dict:

```python
@pytest.mark.asyncio
async def test_options_flow_persists_preset_fan_mode_and_switches(hass):
    """Sleep preset fan_mode + switches persist through the presets step.

    Regression coverage for the preset-driven fan mode / feature switches
    feature: the options flow must transform `{preset}_fan_mode` and
    `{preset}_switches` form fields into the nested preset config dict.
    """
    from custom_components.dual_smart_thermostat.feature_steps.presets import (
        PresetsSteps,
    )

    steps = PresetsSteps()
    transformed = steps._transform_preset_fields_to_new_format(
        {
            "sleep_temp": 18,
            "sleep_fan_mode": "quiet",
            "sleep_switches": ["switch.office_ac_sleep_2"],
        }
    )

    assert transformed["sleep"]["fan_mode"] == "quiet"
    assert transformed["sleep"]["switches"] == ["switch.office_ac_sleep_2"]

    # And the round-trip back to form fields re-flattens for display.
    flattened = steps._flatten_presets_for_form({"sleep": transformed["sleep"]})
    assert flattened["sleep_fan_mode"] == "quiet"
    assert flattened["sleep_switches"] == ["switch.office_ac_sleep_2"]
```

- [ ] **Step 2: Run test to verify it passes**

Run: `./scripts/docker-test "tests/config_flow/test_options_flow.py::test_options_flow_persists_preset_fan_mode_and_switches" -v`
Expected: PASS (Tasks 6–7 already implement the behavior).

> If it FAILS, the transform/flatten maps from Task 7 are not wired correctly —
> fix Task 7 before continuing.

- [ ] **Step 3: Commit**

```bash
git add tests/config_flow/test_options_flow.py
git commit -m "test: persistence of preset fan_mode + switches in options flow"
```

---

### Task 10: Full verification (lint + tests)

**Files:** none (verification only)

- [ ] **Step 1: Lint everything**

Run: `./scripts/docker-lint`
Expected: all of isort, black, flake8, codespell, ruff pass. If anything fails, run `./scripts/docker-lint --fix`, re-run, and commit the formatting fixes.

- [ ] **Step 2: Run the focused suites**

Run: `./scripts/docker-test tests/presets/ tests/config_flow/test_options_flow.py tests/config_flow/test_translations.py -q`
Expected: PASS.

- [ ] **Step 3: Run the full test suite**

Run: `./scripts/docker-test`
Expected: PASS — confirm the new climate constructor parameter did not break any system-type setup.

- [ ] **Step 4: Commit any lint fixes**

```bash
git add -A
git commit -m "style: lint fixes for preset actions feature"
```

---

## Self-review notes (for the implementer)

- **Spec coverage:** fan_mode (Tasks 1,2,4,6), switches on-only (Tasks 1,2,4,6), restore previous state (Task 3,4), restart persistence (Task 5), all-presets generic (schema/transform loop over every selected preset, Tasks 6–8), config+reconfigure+options integration (Tasks 6–9), graceful degradation (Task 2/3 unavailable + unsupported handling), backward compatibility (empty fields dropped in Task 7; matching logic untouched).
- **Out of scope (per design):** swing mode, separate turn-off list, native AC `preset_mode` — none implemented, intentionally.
- **Type/name consistency:** `PresetActionManager(hass, features)`; methods `async_apply(preset_env)`, `async_restore()`, `serialize_baseline()`, `restore_baseline(data)`; `PresetEnv.has_fan_mode()/has_switches()`, attrs `fan_mode`/`switches`; const `CONF_PRESET_SWITCHES="switches"`, `ATTR_PRESET_ACTION_BASELINE="preset_action_baseline"`; switches driven via `homeassistant.turn_on/turn_off`. These names are used identically across all tasks.
- **Manual sanity check (optional, real HA):** select Sleep on the combined AC climate entity and confirm `climate.<wrapped>` fan mode becomes `quiet` and `switch.office_ac_sleep_2` turns on; clear the preset and confirm both revert.
