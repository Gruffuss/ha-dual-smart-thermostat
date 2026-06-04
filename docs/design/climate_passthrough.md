# Design Plan: Climate-Entity Passthrough ("Smart Climate" device)

## Goal

Let `dual_smart_thermostat` control a real **`climate` entity** (e.g. a Gree split
AC) directly — sending `hvac_mode`, target temperature, **fan speed**, and **swing**
to the underlying device and mirroring its capabilities upward — while keeping this
integration's orchestration extras (presets, openings, scheduling). This is the
"combination of both" of `dual_smart_thermostat` + `better_thermostat`.

### Why this is needed

Today the integration only drives **on/off switches**. For a Gree AC the user has
bridged it with a binary switch, which throws away everything the Gree exposes
(modes, fan speed, swing). The capabilities the user wants already live on the Gree
`climate` entity — the integration just has no way to talk to it.

The current control path assumes a switch in four places:

| Concern | Switch device (today) | Wrapped climate device (new) |
|---|---|---|
| Turn on/off | `homeassistant.turn_on/off` (`generic_hvac_device.py:316,347`) | `climate.set_hvac_mode` |
| "Is active?" | state `== STATE_ON` (`generic_controller.py:62`) | wrapped `hvac_action` / `hvac_mode` |
| Temperature | bang-bang on the switch | `climate.set_temperature` passthrough |
| Fan / swing | none (fan only via `fan.*`, `fan_device.py:142`) | `climate.set_fan_mode` / `set_swing_mode` |

## Config shape decision: **new system type** `smart_climate`

Two options were considered:

1. **Extend existing types** — allow `CONF_HEATER`/`CONF_COOLER` to be a `climate`
   entity and branch each device class on domain.
   - ✗ Forces switch-vs-climate branching into every existing device/controller
     (`is_active`, command path, hvac_action) — fragile, high regression risk,
     spreads the new behavior across the whole device layer.

2. **New system type `smart_climate`** (RECOMMENDED) — a dedicated device class with
   its own runtime, selected by the factory.
   - ✓ Runtime behavior (delegate to a climate entity) is genuinely different from
     bang-bang switching; isolating it is cleaner and lower-risk.
   - ✓ Reuses existing `feature_steps/` for openings & presets.
   - ✓ Clear mental model for users: "I have a smart AC/heat pump that already speaks
     `climate`."

Add to `SystemType` enum (`const.py:30`) and `SYSTEM_TYPES` dict (`const.py:50`):

```python
SMART_CLIMATE = "smart_climate"
# ...
SystemType.SMART_CLIMATE: "Smart Climate (Wrapped AC / Heat Pump)",
```

New config key: `CONF_CLIMATE_ENTITY = "climate_entity"` (the wrapped entity).

## Architecture

### New device: `hvac_device/wrapped_climate_device.py`

`WrappedClimateDevice(ControlableHVACDevice)` — does NOT extend
`GenericHVACDevice` (whose whole contract is switch on/off). It implements the
`ControlableHVACDevice` interface directly and delegates to the wrapped entity.

Responsibilities:

- **Capability discovery** at setup — read the wrapped entity's attributes:
  `hvac_modes`, `fan_modes`, `swing_modes`, `min_temp`/`max_temp`/`target_temp_step`,
  and `supported_features`. Cache them. Re-read on wrapped-entity state change so a
  device that becomes available late is picked up.
- **Commands** (all via `hass.services.async_call("climate", ...)`):
  - `async_set_hvac_mode` → `climate.set_hvac_mode`
  - `async_set_temperature` → `climate.set_temperature`
  - `async_set_fan_mode` → `climate.set_fan_mode` (guarded by capability)
  - `async_set_swing_mode` → `climate.set_swing_mode` (guarded by capability)
- **State/feedback reads** from the wrapped entity:
  - `is_active` → wrapped `hvac_action in (HEATING, COOLING, DRYING, FAN)` when the
    attribute exists, else `hvac_mode != OFF`. (Fixes the `STATE_ON` mismatch.)
  - `hvac_action` → passthrough of the wrapped entity's `hvac_action`.
  - `current_fan_mode` / `current_swing_mode` → wrapped current attributes.
- **min cycle duration / openings** still honored at this layer (don't re-issue if an
  opening is open → command `set_hvac_mode(off)`).

### Factory wiring (`hvac_device/hvac_device_factory.py`)

In `create_device` (line 73), when `system_type == SMART_CLIMATE`, build and return a
`WrappedClimateDevice` for `CONF_CLIMATE_ENTITY` and skip the switch-based branches.

### Feature manager (`managers/feature_manager.py`)

Mirror the existing fan-mode delegation pattern (`feature_manager.py:336-357`) for the
wrapped device:

- `supports_fan_mode` / `fan_modes` → delegate to wrapped device capabilities.
- New `supports_swing_mode` / `swing_modes` → same pattern.
- In `set_support_flags` (around `feature_manager.py:297`): OR in
  `ClimateEntityFeature.FAN_MODE` and `SWING_MODE` (and `TARGET_TEMPERATURE` /
  `SWING_HORIZONTAL_MODE` if the wrapped entity reports it) based on the wrapped
  entity's `supported_features`.

### Climate entity (`climate.py`)

- `fan_mode` / `fan_modes` / `async_set_fan_mode` already exist
  (`climate.py:1135,1146,1361`) — route them to the wrapped device when present.
- **New**: `swing_mode`, `swing_modes`, `async_set_swing_mode` (mirror the fan-mode
  trio). Persist `swing_mode` in `extra_state_attributes` and restore it.
- `hvac_modes` — when a wrapped device is present, expose the **intersection** of what
  the user enabled and what the wrapped entity supports.
- `supported_features` — merge wrapped flags (see feature manager).

### Constants / schemas / translations

- `const.py`: `CONF_CLIMATE_ENTITY`, `ATTR_SWING_MODE`, `ATTR_SWING_MODES`,
  `SystemType.SMART_CLIMATE`, `SYSTEM_TYPES` entry.
- `schemas.py`: new core schema for `smart_climate` whose entity selector is
  `[CLIMATE_DOMAIN]`; a sensor for room temperature still applies (the whole point is
  using a better-placed sensor, Better-Thermostat-style).
- `translations/en.json`: new system type label + any new step/field strings.

## Config flow integration (mandatory per CLAUDE.md)

- **config_flow.py**: add `smart_climate` to system-type selection; core step collects
  `CONF_CLIMATE_ENTITY` + room temp sensor; then reuse existing feature steps for
  **openings** and **presets** (floor/two-stage/aux do not apply and are skipped).
- **options_flow.py**: allow changing the wrapped entity, openings, presets.
- **reconfigure**: support switching to/from `smart_climate`.
- Step ordering rule still holds: openings → presets last.

## Phasing (each independently testable)

1. **Phase 1 — Control path.** New system type + `WrappedClimateDevice` doing
   `set_hvac_mode` + `set_temperature` + correct `is_active`/`hvac_action` from the
   wrapped entity. Config/options/reconfigure flow + tests. *This alone replaces the
   fake-switch setup with real control.*
2. **Phase 2 — Fan speed passthrough.** `climate.set_fan_mode`, mirror `fan_modes`,
   expose `FAN_MODE`. Tests.
3. **Phase 3 — Swing passthrough.** New `swing_mode` trio on the climate entity +
   `climate.set_swing_mode`, mirror `swing_modes`, `SWING_MODE` flag, persistence.
   Tests. (Optionally `SWING_HORIZONTAL_MODE` on newer HA.)
4. **Phase 4 — Feedback polish.** Full `hvac_action` passthrough, availability
   handling when the wrapped entity is unknown/unavailable.

## Testing

- New `tests/test_smart_climate_mode.py` — capability detection, command routing
  (assert `climate.set_*` service calls), `is_active`/`hvac_action` from wrapped state,
  fan/swing set + restore, opening pauses → `set_hvac_mode(off)`.
- `tests/config_flow/` — new `test_smart_climate_*` for config/options/reconfigure and
  feature integration (openings, presets), following the consolidated-test guidance.
- Run via `./scripts/docker-test` and `./scripts/docker-lint` before commits.

## Key risks / caveats

- **Redundant control logic**: the Gree already does closed-loop temperature control,
  so this integration's bang-bang cycling is mostly bypassed for this device — value is
  the orchestration layer on top. Expected and acceptable.
- **HA version differences**: `SWING_HORIZONTAL_MODE` and some swing semantics are
  newer; gate on `supported_features`.
- **Availability**: wrapped entity may be `unavailable`/`unknown`; guard all reads and
  commands.
- **Backward compatibility**: purely additive — a new system type, no change to
  existing switch-based behavior.

## Upstream note

This is a fork (`gruffuss/ha-dual-smart-thermostat`) of `swingerman/...`. The feature
is sizable and changes the integration's scope; consider an upstream discussion/issue
before a PR if the intent is to merge back.
