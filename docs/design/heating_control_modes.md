# Design: Native heating control modes — TPI, AI Time Based, Valve Maintenance

## Goal

Drive a heating **valve/switch** directly with proportional + self-learning control
and keep the valve from seizing — natively, so the underfloor loop no longer needs to
be wrapped in a Generic Thermostat + Better Thermostat, and the external AppDaemon TPI
hop can be dropped.

Three features, adapted from [Better Thermostat](https://github.com/KartoffelToby/better_thermostat):

1. **TPI** — time-proportional duty cycle (`utils/calibration/tpi.py`).
2. **AI Time Based** — heating-power learning control (`utils/thermal_learning.py` +
   `heating_power_valve_position` in `utils/helpers.py`). **User priority.**
3. **Valve maintenance** — periodic anti-stick exercise (`utils/valve_maintenance.py`).

### The core adaptation: TRV valve-% → on/off PWM

Better Thermostat targets **TRVs** that accept a *valve-position %* or a *calibrated
target temp*. Our heater is a bare **on/off switch**. Every BT control mode ultimately
produces a number in **0–100 %**; on a switch that becomes a **PWM duty cycle**: each
`cycle_time`, hold the switch **ON for `duty% × cycle_time`**, then **OFF** for the
remainder. No calibration-offset math is needed (we own the actuator directly).

This is heater-side only and independent of the climate-entity cooler work; it applies
to any heater/valve switch (`simple_heater`, the heater half of `heater_cooler`, etc.).

---

## Shared component: the PWM duty-cycle engine

A new controller turns a **duty % (0–100)** into switch ON/OFF timing.

- `cycle_time` (config) — full PWM period. Underfloor/slabs are slow → default ~30 min;
  fast electric radiators → shorter. Range-validated.
- Each cycle: `on_time = duty% × cycle_time`, then off for the rest. `duty=0` → stay off;
  `duty=100` → stay on (effectively bang-bang at the extremes).
- **Actuator safety**: honor `min_cycle_duration` as a floor on on/off segments; clamp
  tiny duties to 0 and near-100 to 100 to avoid chattering the relay.
- Driven by a dedicated timer (`async_track_point_in_time` re-armed each segment), not the
  coarse `keep_alive`. Recomputes `duty%` at each cycle boundary from the active mode.
- Respects openings (open → off), `hvac_mode == OFF`, and floor-temp protection exactly
  like the existing controllers.

The mode plugs in a **duty function**: `bang_bang` (existing behavior, duty∈{0,100}),
`tpi`, or `ai`.

---

## Mode 1 — TPI

versatile_thermostat / BT formula:

```
duty% = clamp( coef_int·(target − current) + coef_ext·(target − outdoor) ) × 100
```

- Params: `tpi_coef_int` (default 0.6), `tpi_coef_ext` (default 0.01), `cycle_time`.
- Overshoot cutoff: if `current − target > threshold_high` → duty 0.
- `outdoor` term optional (reuses existing `CONF_OUTSIDE_SENSOR`).
- Good for fast-responding systems; cheapest mode and a clean proof of the PWM engine.

## Mode 2 — AI Time Based (priority)

Two EMA learners (ported from `thermal_learning.py`), plus the BT control law:

- **HeatingPowerTracker** — on each heat→peak cycle, learn effective **heating power
  (°C/min)** via EMA, weighted by setpoint position and outdoor gradient. Bounded
  `[0.005, 0.2]` °C/min.
- **HeatLossTracker** — during idle periods, learn **heat-loss rate (°C/min)**, EMA,
  bounded `[0.001, 0.05]`. Used for anticipation/idle reasoning.
- **Control law** (`heating_power_valve_position`):
  ```
  valve% = a·(error / heating_power)^b        # a=0.019, b=0.946
  ```
  with minimum-opening floors when `error` is meaningful, clamped 0–1 → ×100 → duty% →
  PWM. Faster-heating room ⇒ lower duty for the same error; slow slab ⇒ higher duty.
- **Cold start & ramp**: seed `heating_power` at the BT default; the BT author notes
  **2–3 days** to optimal. Optional `ai_initial_heating_power` config for a head start.
- **Persistence**: learned `heating_power` / `heat_loss` must survive restarts — saved in
  `extra_state_attributes` and restored via `state_manager` (mirror the fan-mode restore
  pattern). A reset service (like BT's `reset_heating_power`) to clear learnings.
- **Learning hooks**: feed the trackers from the existing sensor-change handler and HVAC
  action transitions (heating ↔ idle), with `now` passed in (tests stay deterministic).

> Note: BT's author recommends **MPC Predictive** for slow underfloor systems, with AI
> Time Based as the "start here" default. MPC (`utils/calibration/mpc.py`) and PID
> (`pid.py`) are out of scope here but could be added later as extra modes on the same
> engine.

## Mode 3 — Valve maintenance (anti-stick)

Port of `valve_maintenance.py`, simplified for a switch:

- Every `valve_maintenance_interval` (default ~7 days) **+ jitter**, run: **ON → wait →
  OFF → wait, ×2**, then restore the prior switch state.
- Runs **regardless of HVAC mode / season** — the whole point is exercising the valve in
  summer when heating is off, so it doesn't seize.
- Independent boolean toggle; works with bang_bang too. First fire randomized after
  startup so multiple instances don't sync up.

---

## Configuration

New keys in `const.py` (+ `schemas.py`, all flows, translations):

- `CONF_HEATER_CONTROL_MODE` = `bang_bang` (default — **backward compatible**) | `tpi` | `ai`
- `CONF_PWM_CYCLE_TIME` (used by `tpi` and `ai`)
- TPI: `CONF_TPI_COEF_INT`, `CONF_TPI_COEF_EXT`
- AI: `CONF_AI_INITIAL_HEATING_POWER` (optional seed); learned values are state, not config
- `CONF_VALVE_MAINTENANCE` (bool) + `CONF_VALVE_MAINTENANCE_INTERVAL`

Flow integration (per CLAUDE.md): an advanced "heating control" section in the heater
config step, surfaced in **config + reconfigure + options** flows; control-mode-dependent
fields shown conditionally. New service `reset_heating_power`.

## Architecture / integration points

- `hvac_controller/` — new `PwmHeaterController` (or extend `HeaterController`) with a
  pluggable duty function; `tpi.py` / `ai_heating.py` pure-logic modules under a new
  `control/` package (ported, HA-free, unit-testable like BT's).
- `managers/` — a learning manager (or extend `environment_manager`/`feature_manager`)
  owning the trackers; persistence via `state_manager`.
- `climate.py` — expose learned values + reset service; wire learning hooks into the
  sensor-change path; PWM timer lifecycle in `async_added_to_hass` / on-remove.
- Reuse existing `CONF_OUTSIDE_SENSOR`, `min_cycle_duration`, openings, floor protection.

## Phasing

1. **PWM engine + TPI** — duty-cycle engine, `tpi` mode, config/flows, tests. Validates
   the engine end-to-end and replaces AppDaemon TPI with a proven formula.
2. **AI Time Based** — learners, `heating_power_valve_position` control law, persistence,
   reset service, tests. *(Priority feature.)*
3. **Valve maintenance** — independent; small. Can also ship first as a quick win.

Each phase is committed and merged green (`./scripts/docker-test`, `./scripts/docker-lint`).

## Risks / notes

- **Actuator wear**: PWM cycles a relay/valve — enforce `min_cycle_duration`, clamp
  extremes, sane default `cycle_time`. Document the trade-off.
- **Backward compatible**: default `bang_bang`; existing configs unchanged.
- **Learning cold-start**: defaults + 2–3 day ramp; expose learned values for visibility.
- **Determinism in tests**: pass `now`/timestamps in (BT's modules are pure and HA-free —
  port that property so duty math and learning are unit-testable without HA).
- **Attribution**: algorithms adapted from Better Thermostat (MIT-compatible) and
  versatile_thermostat; credit in code + docs.
