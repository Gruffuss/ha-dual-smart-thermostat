# Design Plan: One climate entity wrapping switch-heating + climate-cooling

## The real goal

**One** `dual_smart_thermostat` climate entity per room that combines two underlying
devices, so the user doesn't juggle multiple climate entities per room:

- **Heating**: an underfloor valve **switch** (on/off), ideally driven by a **TPI /
  time-proportional** algorithm so the external AppDaemon TPI hop can be dropped.
- **Cooling**: a **Gree `climate` entity**, with **fan speed + swing passthrough**.

This is the "combination of both" the user described: `dual_smart_thermostat`'s
single-entity dual heat/cool + presets/openings orchestration, plus
`better_thermostat`'s two missing pieces — wrapping a real climate entity and TPI
control of a valve.

## Config shape decision: **extend `heater_cooler`** (NOT a new system type)

(Revised from the first draft, which proposed an isolated `smart_climate` type — wrong
for this use case.)

The existing `heater_cooler` system type already composes a heater device + cooler
device through `HeaterCoolerDevice(MultiHvacDevice)`
(`hvac_device/heater_cooler_device.py:17`), which routes `HVACMode.HEAT`→heater and
`HVACMode.COOL`→cooler purely by each sub-device's `hvac_modes`
(`multi_hvac_device.py`). So:

- **heater** = underfloor valve **switch** (existing `HeaterDevice`, optionally with
  new TPI controller)
- **cooler** = Gree **climate entity** (new `WrappedClimateDevice` reporting
  `hvac_modes = [COOL, OFF]`)

Allowing the cooler slot to be a climate entity = adding `CLIMATE_DOMAIN` to the
selector and swapping the device built by the factory. No new system type, full reuse
of feature steps (openings, presets), minimal config-flow churn.

Schema change (`schemas.py:409-445` `get_heater_cooler_schema` and `~823-826`
`get_core_schema`):

```python
from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
# CONF_COOLER selector:
get_entity_selector([SWITCH_DOMAIN, INPUT_BOOLEAN_DOMAIN, CLIMATE_DOMAIN])
```

New config key: `CONF_COOLER` stays; add nothing new for the entity itself. (Heater
stays switch-only for now.)

---

## Track 1 — Climate-entity cooler with fan/swing passthrough

### New device: `hvac_device/wrapped_climate_device.py`

`WrappedClimateDevice(ControlableHVACDevice)` — implements the
`ControlableHVACDevice` interface directly (NOT `GenericHVACDevice`, whose contract is
switch on/off) and delegates to the wrapped climate entity.

- `hvac_modes` → `[HVACMode.COOL, HVACMode.OFF]` (so `MultiHvacDevice` routes COOL to
  it). Capabilities (`fan_modes`, `swing_modes`, temp range, `supported_features`) read
  from the wrapped entity's attributes at setup and refreshed on its state change.
- **Commands** via `hass.services.async_call("climate", ...)`:
  - turn on / off / set mode → `climate.set_hvac_mode` (cool / off)
  - `async_set_temperature` → `climate.set_temperature`
  - `async_set_fan_mode` → `climate.set_fan_mode` (guarded by capability)
  - `async_set_swing_mode` → `climate.set_swing_mode` (guarded by capability)
- **Feedback reads** from the wrapped entity (fixes the `STATE_ON` mismatch in
  `generic_controller.py:62` — a climate entity's state is its hvac_mode, never `"on"`):
  - `is_active` → wrapped `hvac_action == COOLING` if present, else `hvac_mode != OFF`
  - `hvac_action` → passthrough of wrapped `hvac_action`
  - `current_fan_mode` / `current_swing_mode` → wrapped current attributes
- Honors openings/min-cycle at this layer (opening open → `set_hvac_mode(off)`).

### Control-loop ownership (cooling)

The Gree does no autonomous control in the user's setup (manual), so **this
integration owns the cooling decision** using the room sensor + tolerance band, and
issues `set_hvac_mode(cool)` / `set_hvac_mode(off)`. Fan/swing are passed through as the
user's selections.

- **Phase-1 strategy**: simple bang-bang on the Gree (cool when above target+tol, off
  when below target-tol) — same tolerance logic the integration already uses.
- **Future enhancement** (Better-Thermostat-style, optional): instead of on/off,
  passthrough a *calibrated setpoint* to the Gree so its inverter modulates, offsetting
  for the difference between the room sensor and the Gree's internal sensor. Better for
  inverter ACs; deferred until the basic path works.

### Factory wiring (`hvac_device_factory.py:266` `_create_cooler_device`)

Detect the cooler entity's domain: if `climate`, build `WrappedClimateDevice`; else the
existing `CoolerDevice`. Keep the `CoolerFanDevice` wrap only for the switch+fan case.
`HeaterCoolerDevice` is then constructed with `[heater_device, wrapped_climate_device]`
unchanged (it identifies sub-devices by `hvac_modes`).

### Feature manager (`managers/feature_manager.py:336-357`, `~297`)

Mirror the existing fan-mode delegation for the wrapped cooler:
- `supports_fan_mode` / `fan_modes` → delegate to the wrapped device.
- **New** `supports_swing_mode` / `swing_modes` → same pattern.
- In `set_support_flags`: OR in `ClimateEntityFeature.FAN_MODE` / `SWING_MODE` (and
  `SWING_HORIZONTAL_MODE` on newer HA) based on the wrapped entity's
  `supported_features`.

### Climate entity (`climate.py`)

- `fan_mode`/`fan_modes`/`async_set_fan_mode` exist (`1135,1146,1361`) — route to the
  wrapped cooler when active.
- **New**: `swing_mode`/`swing_modes`/`async_set_swing_mode` (mirror the fan trio),
  persisted in `extra_state_attributes` and restored.
- `supported_features` merges wrapped flags via the feature manager.

### Consts / translations

- `const.py`: `ATTR_SWING_MODE`, `ATTR_SWING_MODES`.
- `translations/en.json`: swing field strings; note climate entity allowed as cooler.

### Phasing — Track 1

1. **Control path**: `WrappedClimateDevice` doing `set_hvac_mode`+`set_temperature`,
   correct `is_active`/`hvac_action`, factory + schema (`CLIMATE_DOMAIN`) + config/
   options/reconfigure flow + tests. *Replaces the fake-switch cooling with real
   control.*
2. **Fan speed passthrough** (`set_fan_mode`, mirror `fan_modes`, `FAN_MODE` flag).
3. **Swing passthrough** (new swing trio + `set_swing_mode`, mirror `swing_modes`).
4. **Polish**: availability/unknown handling; optional calibrated-setpoint strategy.

---

## Track 2 — TPI control for the underfloor heating switch (optional / separate)

Today: the integration emits "heat needed" and an external AppDaemon applies TPI. Goal:
native TPI so the valve switch is driven proportionally, dropping the AppDaemon hop.

**Status in codebase**: no PWM/TPI/proportional/duty-cycle code exists (grep: 0 hits).
Control is pure bang-bang with a tolerance hysteresis band
(`heater_controller.py:39-77`, `environment_manager.py:367-442`). **But** the
`keep_alive` timer (`climate.py:850`, default 300s) already provides the periodic tick a
duty cycle needs.

**Design sketch** (net-new, sizable):
- New config: `CONF_HEAT_CONTROL_MODE` (`bang_bang` | `tpi`) + TPI params
  (`tpi_cycle_time`, gains `tpi_coef_int`/`tpi_coef_ext` or a single proportional band).
- New `TpiController` (or extend `HeaterController`): each `keep_alive` cycle, compute
  duty = clamp(K * (target - room) [+ outdoor term], 0..1), then switch the valve ON
  for `duty * cycle_time` and OFF for the remainder within the cycle.
- Config/options-flow integration (control-mode step), translations, tests.
- Reference the user's AppDaemon TPI repo to match coefficients/behavior.

This is independent of Track 1 and can ship later. **Open question: include TPI now, or
ship Track 1 first and keep AppDaemon TPI for the moment?**

---

## Testing & quality

- `tests/test_wrapped_climate_cooler.py` — capability detection, `climate.set_*` call
  assertions, `is_active`/`hvac_action` from wrapped state, fan/swing set+restore,
  opening → `set_hvac_mode(off)`.
- `tests/config_flow/` — heater_cooler with a climate-entity cooler (config/options/
  reconfigure + feature integration), following consolidated-test guidance.
- If Track 2: `tests/features/test_tpi_heating.py` — duty-cycle math + timed toggling.
- Run `./scripts/docker-test` and `./scripts/docker-lint` before each commit.

## Risks / notes

- **Backward compatible**: additive — existing switch coolers and heater_cooler configs
  unchanged.
- **Availability**: wrapped entity may be `unavailable`/`unknown`; guard reads/commands.
- **HA version**: gate `SWING_HORIZONTAL_MODE` and swing semantics on
  `supported_features`.
- **Inverter ACs**: bang-bang cycling a compressor is acceptable but not ideal; the
  calibrated-setpoint enhancement is the better long-term path.
- **Upstream**: this is a fork of `swingerman/...`; consider an upstream discussion
  before a merge-back PR given the size.
