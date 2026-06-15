# Design: Preset-driven fan mode + feature switches

**Date:** 2026-06-15
**Status:** Approved (design phase)

## Problem

Presets in the Dual Smart Thermostat currently only override the *target environment*
(temperature, humidity, floor-temp limits). A user wants the **Sleep** preset (and
presets in general) to also:

1. Set the wrapped AC's **fan speed** (e.g. `quiet`).
2. Turn on the AC's native **Sleep** function.

Investigation of the user's hardware (Gree-style AC via the SmartIR/Gree integration)
established:

- `climate.office_ac` exposes `fan_modes` including `quiet`. `supported_features = 937`
  (`TARGET_TEMPERATURE | FAN_MODE | SWING_MODE | TURN_OFF | TURN_ON | SWING_HORIZONTAL_MODE`).
  It does **not** expose `PRESET_MODE`.
- The AC's extra functions (Sleep, Power Save, X-Fan, Health, Lights, Beeper, …) are
  exposed as **separate `switch` entities**, e.g. `switch.office_ac_sleep_2`.

So "quiet" is a fan mode on the wrapped climate entity, and "AC sleep" is a separate
switch entity that the preset must toggle.

## Goals

- Any preset can optionally set a **fan mode** on the wrapped AC climate entity.
- Any preset can optionally turn **on** a list of external `switch`/`input_boolean`
  entities while it is active.
- Leaving the preset **restores** the fan mode and switch states that were in effect
  before the preset activated.
- Restore is robust across a Home Assistant restart while a preset is active.
- Fully backward compatible: presets without these fields behave exactly as today.

## Non-goals

- Swing mode control from presets (explicitly out of scope for this iteration; can be
  added later via the same mechanism using the existing `async_set_swing_mode`).
- A separate "turn OFF" switch list (on-only; restore-on-exit handles reverting).
- Native AC `preset_mode` pass-through (the hardware does not expose `PRESET_MODE`).

## Approach

**Extend `PresetEnv` with two optional fields and add a dedicated `PresetActionManager`**
that applies them and remembers the pre-preset baseline for restore. Chosen over
(a) cramming the logic into `PresetManager` (which lacks device/service references and
would grow muddy) and (b) delegating to a user-authored HA automation (the user wants
this built into the preset).

## Components

### 1. `PresetEnv` (preset_env/preset_env.py)

Add two optional attributes, parsed from kwargs like the existing ones:

- `fan_mode: str | None` — fan mode string to apply to the wrapped climate entity.
- `switches: list[str] | None` — entity_ids (`switch.*` / `input_boolean.*`) to turn on
  while the preset is active.

Add helpers `has_fan_mode()` and `has_switches()`. These fields are **not** part of the
target-environment matching used by `find_matching_preset()` (auto-preset selection stays
keyed on temperature/humidity/floor limits only), so `_values_match_preset` is unchanged.

### 2. `PresetActionManager` (managers/preset_action_manager.py — new)

Owns application + restore of preset *actions* (fan mode + switches). Holds the saved
baseline.

Responsibilities:

- `async_apply(preset_env)`:
  1. If a baseline is not already held (i.e. we are entering from no-action state),
     capture the current fan mode (from the wrapped climate device) and the current
     state of each switch the preset references.
  2. Apply `preset_env.fan_mode` via the resolved wrapped-climate device's
     `async_set_fan_mode`.
  3. Turn on each switch in `preset_env.switches` via `switch.turn_on`
     (`homeassistant.turn_on` for `input_boolean`).
- `async_restore()`:
  1. Restore the saved fan mode (if any) via the device's `async_set_fan_mode`.
  2. Restore each saved switch to its prior on/off state.
  3. Clear the held baseline.
- `serialize_baseline()` / `restore_baseline(data)`: expose the baseline so the climate
  entity can persist/restore it via `extra_state_attributes` across restarts.

Dependencies (constructor injection): `hass`, a callable/reference to resolve the wrapped
climate (cooler) device, and the `FeatureManager` (to gate on whether fan-mode is
supported). The device is resolved lazily at apply time because availability can change.

Graceful degradation:

- No wrapped-climate device present, or device does not support fan mode, or the fan mode
  is not in the device's `fan_modes` → skip fan-mode application with a warning; switches
  still apply.
- A referenced switch is unavailable/unknown → skip that switch with a warning; others
  still apply, and an unavailable switch is not captured into the baseline (so restore
  does not try to drive it).

### 3. Climate entity wiring (climate.py)

In `async_set_preset_mode()` (after the existing temperature/humidity application,
around the `_async_control_climate(force=True)` call):

- Determine transition using `old_preset_mode` vs `preset_mode`:
  - Leaving to `PRESET_NONE` → `await preset_actions.async_restore()`.
  - Entering a preset that has actions → `await preset_actions.async_apply(preset_env)`.
  - preset → preset → `async_restore()` then `async_apply(new preset_env)`.
- Persist/restore baseline:
  - Add the serialized baseline to `extra_state_attributes`
    (new `ATTR_PRESET_ACTION_BASELINE` in const.py).
  - On `apply_old_state` / startup restore, if a preset was active and a baseline is
    present, hand it back to the manager via `restore_baseline()` so a later exit reverts
    correctly.

The same apply/restore is also invoked from the presence-driven away/restore paths that
already call `async_set_preset_mode` indirectly — no extra wiring needed since they route
through the same method.

### 4. Configuration flow (schemas.py, feature_steps/presets.py, options_flow.py, translations)

Per the repo's mandatory flow-integration rule, surface the two fields in config,
reconfigure, and options flows:

- **`{preset}_fan_mode`** — already has translation labels (currently dead). Wire it to a
  real selector. When the configured cooler is a climate entity whose `fan_modes` are
  known, present a dropdown of those modes; otherwise a free-text field. Validation:
  optional string; empty → omitted.
- **`{preset}_switches`** — new entity selector (multi-select, domains `switch` +
  `input_boolean`). Empty → omitted.
- Extend the suffix→attribute maps in `feature_steps/presets.py`
  (`_flatten_presets_for_form` and `_transform_preset_fields_to_new_format`) with
  `_fan_mode → fan_mode` and `_switches → switches`.
- Add translation keys for `{preset}_switches` (labels + descriptions) for every preset,
  matching the existing per-preset pattern.

### 5. Constants (const.py)

- `ATTR_PRESET_ACTION_BASELINE = "preset_action_baseline"` — extra-state-attribute key.
- No new `CONF_*` top-level keys are required: preset action fields live inside each
  preset's nested dict (e.g. `{"sleep": {"temperature": 18, "fan_mode": "quiet",
  "switches": ["switch.office_ac_sleep_2"]}}`), consistent with the existing new-format
  preset storage.

## Data flow (Sleep example)

```
User selects "Sleep"
  → climate.async_set_preset_mode("sleep")
      → presets.set_preset_mode("sleep")           # preset_env now = sleep
      → environment.set_temperatures_from_...        # existing: target temp = 18
      → preset_actions.async_apply(sleep_env)
          baseline = { fan_mode: "low",
                       switches: { "switch.office_ac_sleep_2": "off" } }
          device.async_set_fan_mode("quiet")         # climate.set_fan_mode
          switch.turn_on(switch.office_ac_sleep_2)
      → _async_control_climate(force=True)           # existing control loop
      → extra_state_attributes now include serialized baseline

User selects "none"
  → climate.async_set_preset_mode("none")
      → preset_actions.async_restore()
          device.async_set_fan_mode("low")           # restore
          switch.turn_off(switch.office_ac_sleep_2)  # was "off" before
```

## Edge cases

- Preset with neither `fan_mode` nor `switches`: `async_apply` is a no-op; baseline stays
  empty; no restore needed.
- Switch-based (non-climate) cooler: fan-mode application is skipped (no wrapped climate
  device); switches still work generically.
- Fan mode not supported by the device: skipped + warning; rest applies.
- HA restart while Sleep active: baseline restored from `extra_state_attributes`; exiting
  the preset still reverts.
- Auto-preset selection (`_check_auto_preset_selection`) sets the preset without going
  through `async_set_preset_mode`; it must also trigger apply/restore. Spec: route its
  preset change through the same apply/restore call (or have it call
  `async_set_preset_mode`) so actions stay consistent.

## Testing

- **Unit (`tests/features/` or `tests/presets/`):** `PresetActionManager` apply/restore;
  baseline capture; serialize/restore_baseline round-trip; unavailable switch and
  unsupported fan-mode handling.
- **Integration (`tests/presets/`):** activating a preset issues the correct
  `set_fan_mode` + `switch.turn_on`; deactivation restores prior fan mode + switch states;
  preset→preset transition restores baseline then applies the new actions; restart-restore
  via state attributes.
- **Config flow (consolidated files per CLAUDE.md):** `{preset}_fan_mode` and
  `{preset}_switches` persist config→options round-trip; reconfigure preserves them;
  deselecting a preset clears them. Add to `test_options_flow.py` and the relevant
  `test_e2e_*_persistence.py`.

## Backward compatibility

- New preset fields are optional; existing presets and YAML configs are unaffected.
- New-format preset dicts simply gain optional keys; old-format presets never set them.
- `find_matching_preset` matching logic is unchanged (actions excluded from matching).
