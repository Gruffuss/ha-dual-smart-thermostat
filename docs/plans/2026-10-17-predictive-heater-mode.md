# Predictive heater control mode: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** a new `heater_control_mode: predictive` that ports the AppDaemon v5 predictive controller into this integration. The thermostat drives the real valve switch directly, owns setpoint, mode, presets and openings, and may start heating early when the forecast says the room will fall below target.

**Why (owner, relayed 2026-10-17):** with bang_bang thermostats switching an AppDaemon demand switch, the controller can only stop heating early, never start early. Demand stays off until T < sp − 0.3 (cold tolerance). Inside the thermostat the planner sees the target directly.

**Spec sources (read-only, AppDaemon repo `C:\tmp_ad\ad`, final v5 = `c761462` on `master`, same tip as `v5-predictive`; live in AppDaemon since 2026-10-17 13:53, 7/7 rooms in `pi` mode):**
- `docs/superpowers/plans/2026-10-17-predictive-heating-v5.md`, the "Design" section (model, learning, forecast, planner, tracker, gate).
- `docs/superpowers/plans/2026-10-17-predictive-heating-v5-results.md` at `c761462`: its "Rulings and deviations", "Ten-day solar mismatch" and "Review regression coverage" sections.
- `adaptive_pid_heating.py` at `c761462`: lines 1-358 are the pure section above `# --- AppDaemon app ---`; the glue is lines 359-981. All `:<line>` citations below are to this commit.
- Tests at `c761462`: `.tests/test_model.py`, `.tests/test_v5_review.py` (the review regressions; there is no `test_review_regressions.py`), `.tests/test_closed_loop.py` and `.tests/test_app.py`.
- Diffed against the plan on 2026-10-17. The pure section is unchanged from the working copy the plan was first written from (one redundant parenthesis), and the changes since are folded in below.

**Prerequisite:** branch `fix/ai-heats-above-target`, which this plan's branch is cut from:
- `bfa605a`: `MultiHvacDevice.call_on_remove_callbacks` forwards to sub-devices, so PWM timers die with the entity.
- `2be8bef`: `PwmHeaterDevice.async_control_hvac` restores `PwmHeaterController.planned_on` when something else switched the heater.

The predictive device subclasses `PwmHeaterDevice` and inherits both.

## Global constraints

- Pure control code lives in `custom_components/dual_smart_thermostat/control/` and imports nothing from Home Assistant. This is the existing rule (`control/pwm.py` docstring; `hvac_controller/pwm_controller.py:1-7`).
- Stdlib only: no numpy. The 3x3 RLS algebra is written out by hand, as in v5.
- Per control pass the cost stays bounded, as in v5's Global Constraints:
  - the planner runs once per cycle boundary: 22 candidates x 240 steps;
  - the per-minute model update is O(6 candidates);
  - forecast sensors use v5's cached path plus an analytic correction (`adaptive_pid_heating.py:787-839`). They never re-simulate every minute.
- Every new config option goes through the config, reconfigure and options flows with real selectors (CLAUDE.md "Configuration Flow Integration"; project memory "no UX shortcuts"). It is also added to `tools/focused_config_dependencies.json`, `tools/config_validator.py` and `docs/config/CRITICAL_CONFIG_DEPENDENCIES.md`.
- Config-flow tests use the real `hass` fixture, not MagicMock (project memory: `StateMachine` has `async_all()`, not `values()`).
- Line endings LF; stage by path; plain commit messages; one branch per wave, cut from `plan/predictive-heater-mode`.
- No time estimates (owner rule).

## Requirements carried from v5 and its fix round

The numbers are v5's. "(a)-(e)" are the heating session's fix-round items, and the plan must include them.

- **Model:** `S <- clamp(S + (v - S)·dt/tau, 0, 1)`; `dT/dt = alpha·S − beta·(T − T_out) + gamma`.
  - Taus are {60, 90, 120, 150, 180, 240}.
  - theta0 is (0.02, 0.001, 0.0); P0 is diag(1e-3, 1e-6, 1e-4); lambda is 0.995.
  - Clamps: alpha [0, 0.2], beta [0, 0.02], gamma [−0.02, 0.02].
  - Blocks are 15 intervals of 16 readings sharing endpoints (results ruling 1; `adaptive_pid_heating.py:165-194`).
  - The active tau is the argmin of err; ties go nearest 120, then the smaller tau (`:217-218`).
- **(e) Covariance and restore:**
  - After each update P is symmetrised and its diagonals capped at 100·P0 by D·P·D scaling (`:196-201`).
  - On restore the slab decays by `exp(−downtime/tau)` (`:964`).
- **Err EWMA:** weight 0.05, seeded with the first squared residual (`:190`).
- **Forecast, hold duty, valve plan, planner:** as `adaptive_pid_heating.py:252-293`.
  - Candidates are {0} ∪ [min_on, cycle].
  - Cost weight is 3 above setpoint, and 0.2 for minutes 0-29.
  - Zero-on is excluded when T < sp and the zero-on forecast never reaches sp.
  - If every candidate stays below sp − 0.3, the cycle is full on.
  - Ties go to the smaller on.
- **Accuracy tracker (`:296-356`), as amended by (b) and (c):**
  - snapshots every 15 valid minutes;
  - re-simulation with the actual valve sequence at 60 and 120 min;
  - EWMA 0.05 seeded with the first sample, for `mae60/120` and `persistence_mae60/120` (persistence = |T_now − T_snapshot|);
  - `n120_independent` counts only 120-min windows starting ≥120 min after the last counted one;
  - the history is 135 minutes, in memory only.
- **Gate (`:724-742`), with (b), (c) and (d):**
  - **Promote** when all of these hold:
    - `n120_independent >= min_validations` (48);
    - `model.blocks >= 672`;
    - `alpha_rel_sd <= 0.25`, where alpha_rel_sd = `sqrt(err·P00)/alpha` (`:203-205`);
    - `mae120 <= mae_on` (0.3);
    - `mae120 <= 0.7 · persistence_mae120 + 1e-12` (the tolerance keeps .28 <= .7·.4 from failing on binary rounding).
  - **Demote** when `mae120 > mae_off` (0.45) or `mae120 > persistence_mae120`.
  - The `auto | off | on` setting works as in v5, except that an active 24 h demotion also overrides a forced `on`, including after a reload (`:729-731`; results ruling "Cold-floor lockout also applies to forced `on`").
- **(a) Cold-room floor:** in predictive mode, T < sp − 0.5 while heating is wanted means the full cycle now, and demotion to the fallback for 24 h (`:637-644`). It still yields to a temperature fault and to a valve exercise (`test_cold_floor_still_yields_to_demand_fault_and_exercise`).
- **Validity of a minute:**
  - a temperature is present;
  - an outdoor value is known (last known is allowed; never invented);
  - no opening is open, and none was in the last 15 min;
  - no valve exercise is running;
  - the AC is off for ≥30 min (`AC_ACTIVE = heat, cool, heat_cool, dry, fan_only, auto`, `:142`);
  - the tick interval is 1 ± 0.05 min (`:711`).
- **Frozen fallback while predictive:**
  - the fallback's error integration is frozen and its duty bank is cleared;
  - its loss learner keeps sampling, under the same AC rule as the model: no samples while the AC is active or in its 30-min cooldown (results: "Apply AC validity to the fallback heat-loss learner too");
  - a completed loss window **does** transfer the integral (`transfer_integral`), so ff + i stays consistent for a later fallback. The final review restored this; the first results file had skipped it (`test_predictive_loss_update_transfers_integral`).
- **Publication never touches the valve:** a failing sensor publication is caught, logged at most every 600 s, and never changes valve state (`:754-765`; `test_publication_failures_never_flap_valves_and_are_rate_limited`).
- **Restore details:** the covariance cap also runs on restore; legacy scalar `blocks` migrate to every candidate; accuracy saved without `persistence_mae120` resets the counts, so new paired scores start (`:220-249`, `:347-356`).

## Design decisions

My decisions are marked **(mine)**. The owner's rulings are in the "Decision log" at the end.

### 1. Where the code lives and how the PWM loop asks for on-minutes

New pure modules:

- `control/predictive_model.py`: a port of `SlabRLS`, `RoomModel`, `forecast`, `hold_duty`, `valve_plan`, `plan_cycle` and `AccuracyTracker`. Names, signatures and numbers are kept identical, so the v5 tests port one to one.
  - **(mine)** The only signature change is `plan_cycle(..., min_off=0)`, which restricts candidates to what the actuator executes. The fork's `control/pwm.py:plan_pwm` collapses an off-segment shorter than `min_cycle_duration` to full on. So the candidate set becomes `{0} ∪ [min_on, cycle − min_off] ∪ {cycle}`, with `min_on = min_off = ceil(min_cycle_duration / 60 s)`. Without this, the planner would score an on of 25 min while `plan_pwm` executes 30, and the tracker would blame the model.
- `control/pi_fallback.py`: the fallback controller (owner, 2026-10-17: a port of v4's PI with heat-loss feed-forward). It ports `DutyBank`, `PIController`, `LossLearner` and `transfer_integral` (`adaptive_pid_heating.py:31-138`).
- `control/predictive.py`: **(mine)** the whole per-minute decision logic as one pure class. The HA device stays thin glue, and the closed-loop simulations run on the pure class at stdlib speed, as v5's ran on the app with a stub clock.

```python
@dataclass
class PredictiveSettings:
    cycle_minutes: int            # pwm_cycle_duration / 60, must be whole
    min_on_minutes: int           # ceil(min_cycle_duration / 60), 0 if unset
    gate: str = "auto"            # "auto" | "off" | "on"
    mae_on: float = 0.3
    mae_off: float = 0.45
    min_validations: int = 48
    min_blocks: int = 672
    kp: float = 65.0              # fallback PI (section 4)
    ki: float = 0.05
    k_loss: float = 2.0

@dataclass
class MinuteInputs:
    now_min: int                  # monotonic minute counter owned by the device
    temp: float | None
    outdoor: float | None         # last known; None until one exists
    valve_on: bool                # switch state over the minute that just ended
    setpoint: float | None        # None = not heating (hvac off / cool / fan / dry)
    valid: bool                   # learning validity (section 2)

class PredictiveController:
    def __init__(self, settings: PredictiveSettings) -> None
    model: RoomModel; tracker: AccuracyTracker; mode: str  # "fallback" | "predictive"
    demoted_until_min: int | None
    def minute(self, inp: MinuteInputs) -> bool
        # update model and tracker; snapshot every 15 valid minutes; run the gate;
        # return True when the running cycle must restart now (cold-room floor, a gate switch)
    def plan(self, inp: MinuteInputs) -> int
        # at a cycle boundary: on-minutes for this cycle (predictive: plan_cycle;
        # fallback: PI + DutyBank; no setpoint -> 0)
    def forecasts(self, inp: MinuteInputs, cycle_elapsed: int, on_minutes: int) -> dict[int, float | None]
        # +30/+60/+120/+180/+240, cached path + analytic correction (v5 :787-826)
    def to_dict(self) -> dict
    @classmethod
    def from_dict(cls, settings: PredictiveSettings, data: dict, downtime_min: float) -> "PredictiveController"
    @classmethod
    def from_appdaemon_room(cls, settings: PredictiveSettings, room: dict, downtime_min: float) -> "PredictiveController"
        # v5 state-file room dict -> controller (section 3)
```

The device glue goes in `hvac_device/predictive_heater_device.py`, `class PredictiveHeaterDevice(PwmHeaterDevice)`, following `AiHeaterDevice` at `hvac_device/ai_heater_device.py:24`:

- `__init__(..., cycle_duration, settings: PredictiveSettings, store_key: str)` creates `self._ctl = PredictiveController(settings)`.
- `_compute_duty()` overrides `pwm_heater_device.py:86`. It returns `100 · self._ctl.plan(inputs) / cycle_minutes`. `PwmHeaterController._async_begin_cycle` (`hvac_controller/pwm_controller.py:96`) already calls `compute_duty` at every boundary, and `plan_pwm` turns the duty back into exactly the planned on-seconds, because the candidates match `plan_pwm`'s collapse rule. **No change to the PWM controller's cycle logic.**
- `async_on_startup` (called from `climate.py:1047`) registers `async_track_time_interval(hass, self._async_minute, timedelta(minutes=1))` with `self.async_on_remove(...)`, and loads the Store (section 3). Since `bfa605a` that callback also fires for a heater inside a `HeaterCoolerDevice`.
- `_async_minute` builds `MinuteInputs` (section 2) and calls `self._ctl.minute(...)`. When that returns True, it calls a new `PwmHeaterController.async_restart()`. That method is the one addition to `pwm_controller.py`: it clears the timer and runs `_async_begin_cycle` now. Because the PWM loop only plans at boundaries, this is how a cold-room floor or a gate switch takes effect at once (v5 sets `cycle_start = now`, `:643`).
- **(mine)** A setpoint change also restarts the cycle. That covers `async_set_temperature`, a preset change and a presence-driven preset, all of which reach the device through `climate._async_control_climate(force=True)`. `async_control_hvac(force=True)` calls `async_restart()` when `force` is set and the setpoint differs from the one planned. The user sees the change act at once instead of up to 30 min later. v5 replans only at boundaries because AppDaemon never saw a setpoint UI.

Factory wiring: `hvac_device_factory.py:355` (`_create_heater_device`) gains a `HeaterControlMode.PREDICTIVE` branch beside the AI branch at `:362`. `const.py:236` (`HeaterControlMode`) gains `PREDICTIVE = "predictive"`. `HVACDeviceFactory(hass, config, feature_manager)` is built at `climate.py:522`; it receives `store_key` (the climate's `sensor_key`: entry_id, CONF_UNIQUE_ID or name, per `const.py:166-168`) through a new keyword argument.

### 2. Demand semantics

- **When it may heat:** the hvac mode is HEAT (target `_target_temp`), or HEAT_COOL / AUTO-dispatched heat (target `_target_temp_low`, per `HeaterDevice.target_env_attr`, `hvac_device/heater_device.py:56-60`). In every other mode `setpoint=None`: the plan is 0 and the device keeps learning (off periods teach beta and gamma, v5 Design "Valid block").
- **No tolerance gating.** `cold_tolerance` and `hot_tolerance` do not apply. The planner's cost decides, including an early start when T ≥ sp but the forecast falls below sp. This is the owner's reason for the port.
- **Heat_cool needs a device change** (verified in the code):
  - `HeaterCoolerDevice._async_auto_toggle` (`hvac_device/heater_cooler_device.py:178-195`) calls the heater only when `too_cold` (T < low − cold_tolerance). Otherwise it calls `async_turn_off_all` at `:195`. So in heat_cool the heater sub-device never sees a pass while the room sits inside the band, and early start cannot happen.
  - `is_cold_or_hot` (`:86`) likewise picks HEATER only while the heater switch is on.
  - **Change (mine):** when `self.heater_device` is a `PredictiveHeaterDevice`, `async_heater_cooler_toggle` (`:156`) calls `heater_device.async_control_hvac` on every pass unless `too_hot` selected the cooler. The cooler logic is unchanged.
  - This also fixes, for predictive, the existing AI/TPI inconsistency in heat_cool: `turn_off_all` switches the heater off mid-segment and the PWM loop switches it back on at the next boundary. Seen in the code, not live; logged as a ledger item for AI/TPI.
- **Openings:** inherited from `PwmHeaterDevice.async_control_hvac` (`pwm_heater_device.py:93`): an open window cancels the PWM and switches off. The minute is invalid while open and for 15 min after close. The tracker's pending windows are invalidated (`:564-567`).
- **Floor protection:** inherited. Floor hot means off; floor cold means forced on, which still counts as valve-on for the slab. The minute stays valid, because the valve state is real.
- **min_cycle_duration:** becomes `min_on = min_off` in whole minutes (section 1). Validation in the schema: `pwm_cycle_duration` is a multiple of 60, and `3 · min_cycle_duration <= pwm_cycle_duration` **(mine)**, so the candidate set is non-empty.
- **Keep-alive and the 60 s poll** (`climate.py:954-973`): they only call `async_control_hvac`, whose enforcement since `2be8bef` keeps the switch on plan. The minute tick is the device's own and does not depend on `min_cycle_duration` being set.
- **hvac_power:** untouched. `PwmHeaterDevice.async_control_hvac` never calls `hvac_power.update_hvac_power`, unlike `GenericHVACDevice` (`generic_hvac_device.py:234`). Predictive keeps that.
- **Valve maintenance:**
  - `climate._set_valve_maintenance_active` (`climate.py:1948`) also calls `self._find_pwm_heater().set_maintenance(active)` when it is a `PredictiveHeaterDevice` (`_find_pwm_heater` is at `climate.py:1307`).
  - While active, minutes are invalid and the tracker is invalidated (v5 `:869`).
  - Since the heater is the real valve after cut-over, the thermostat's own Sunday maintenance replaces AppDaemon's exercise.
- **Temperature fault** (cur_temp None after a valid reading, or `_sensor_stalled`, `climate.py:695`):
  - Fallback PI: feed-forward only, as v4.
  - Predictive: the planner does not run, and the fallback's ff-only duty is used for that cycle.
  - At startup, before the first reading, it stays off as today (`pwm_heater_device.py` "Don't start cycling until we have a current temperature").

### 3. Persistence, restart, migration

- **Store (mine):** `homeassistant.helpers.storage.Store(hass, 1, f"{DOMAIN}.predictive.{store_key}")`.
  - It holds `PredictiveController.to_dict()`: the model per tau (slab, theta, P, err, blocks), active_tau, the tracker scores and counts, mode, demoted_until (as an aware UTC ISO time), the fallback's k_loss, integral and k_updates, and `saved_at`.
  - Writes use `async_delay_save(..., 300)`, plus an immediate `async_save` on a gate switch and in the remove callback.
  - This replaces restoring from state attributes, which is how `_restore_ai_learning` works (`climate.py:1323`): a P matrix does not belong in attributes.
- **Restart:**
  - Load in `async_on_startup`.
  - `from_dict(..., downtime_min = now − saved_at)` decays each slab by `exp(−downtime/tau)` (fix-round (e)).
  - The tracker history is not stored, so 60-min scores resume after 60 min and 120-min scores after 120 (results ruling 10).
  - The gate mode is restored when `gate == auto`.
  - Invalid or non-finite data means a fresh start with a WARNING, as at `adaptive_pid_heating.py:979`. `RoomModel.from_dict` caps the covariance on load, as v5 does.
- **Migration from AppDaemon:**
  - A new service `dual_smart_thermostat.import_predictive_state`, with fields `entity_id`, `path` and `room`. It reads the v5 state file (`"version": 5`, `rooms.<room>` as written by `save_state`, `adaptive_pid_heating.py:902-911`) in an executor job, and refuses a path that `hass.config.is_allowed_path` rejects.
  - It calls `from_appdaemon_room`, which maps slab, theta, P, err and blocks per tau, the tracker scores, `mode` ("pi" becomes "fallback"), `demoted_until`, `k_loss`, `integral` and `k_updates`. The downtime runs from `model_saved_at`.
  - It saves the Store and restarts the cycle.
  - Unverified: whether HA core can read `/addon_configs/...`, where the AppDaemon file lives. The cut-over (section 8) copies the file into `/config/` over Samba first, so it does not depend on that.
  - A v4 file is refused (no model).

### 4. Accuracy gate and fallback; what HA shows

- The gate is exactly as "Requirements". Settings: `predictive_gate` (`auto | off | on`), `predictive_mae_on`, `predictive_mae_off` and `predictive_min_validations`. The 672-block minimum is a constant, not a setting **(mine)**: the owner asked for 7 days, not a knob.
- **Fallback controller (owner, 2026-10-17): a port of v4's PI with heat-loss feed-forward.** That is PIController + LossLearner feed-forward + DutyBank, with kp/ki per room from `apps.yaml`: 65/0.05, living_room 45/0.05.
  - v5's gate and its closed-loop numbers are measured against it, and it is what drives the valves today through AppDaemon.
  - Learning needs ≥7 days before any promotion, so the fallback runs the room that whole time.
  - Not used: bang_bang (`HeaterHvacConroller`, `heater_device.py:45`), which reproduces the late start; and the fork's fixed TPI (`control/tpi.py`), whose coefficients are untuned.
- **Entities (mine): sensor entities, not attributes,** so the recorder keeps history and long-term statistics.
  - They are created by `sensor.py` only when `heater_control_mode == predictive`, as `EntityCategory.DIAGNOSTIC` entities with `unique_id = f"{sensor_key}_predictive_{key}"`.
  - Each listens to a dispatcher signal `SET_PREDICTIVE_SENSOR_SIGNAL.format(sensor_key)`, the same pattern as `HvacActionReasonSensor` (`sensor.py`, `SET_HVAC_ACTION_REASON_SENSOR_SIGNAL` at `const.py:169`).
  - The device sends one dict per minute, after the valve decision. The send is wrapped so that a sensor failure is logged (at most every 600 s) and never reaches the PWM loop, matching v5's publication isolation.
  - The keys are:
    - `forecast_30`, `forecast_60`, `forecast_120`, `forecast_180`, `forecast_240`: °C, temperature device class, measurement state class;
    - `mae_60`, `mae_120`, `persistence_mae_120`: °C, measurement;
    - `mode`: enum `fallback` / `predictive`, with `demoted_until` as an attribute;
    - `alpha_rel_sd`, `slab`, `tau` (min), `planned_on_minutes`, `validations_120_independent`, `learning_blocks`.
  - That is 15 entities per room.
  - The climate itself also gets attributes `predictive_mode` and `heater_duty_cycle`, the latter is existing (`climate.py:1385`).
  - YAML climates get the sensors through the same discovery path as the action-reason sensor (`climate.py:381-385`).
- **Reset service:** `reset_heating_power` (`climate.py:1333`) also resets a predictive controller to a fresh model and the fallback mode **(mine)**, so there is one reset for both learning modes.

### 5. AC interplay (heater_cooler)

- The cooler device of a `*_combined_ac` thermostat is the Gree AC (a `WrappedClimateDevice`, `hvac_device_factory.py:299-330`). So there is **no `ac_entity` key**: the device reads its sibling's wrapped-entity state, `cooler_device._entity_state.state` in AC_ACTIVE.
  - `HeaterCoolerDevice` passes `cooler_device` to the heater at construction: a new `set_sibling_cooler(device)` called in `heater_cooler_device.py:__init__`.
  - Minutes are invalid while the AC is active and for 30 min after (v5 `:573-580`). The tracker is invalidated.
- The AC running does not stop predictive heating, as in v5. In heat_cool the heater's target is `target_temp_low`, so the planner only fights the AC if the band is narrow. That is a configuration matter.
- **No heat-then-cool interlock** (owner, 2026-10-17). The cooler logic in `HeaterCoolerDevice` is unchanged; see "Ideas".

### 6. Config, reconfigure and options flow; translations

- **`schemas.py:get_heating_control_fields`** (`:324`), used by the simple_heater advanced section (`:490`), heater_cooler (`:558`) and the options core schema (`:992`):
  - add `{"value": "predictive", "label": "Predictive (learned floor lag)"}` to the select at `:343-347`;
  - add `predictive_gate` (select auto/off/on), `predictive_mae_on` (number 0.05-2, step 0.05, °C), `predictive_mae_off` (number 0.05-2, step 0.05, °C) and `predictive_min_validations` (number 12-500, step 1);
  - for the PI fallback: `predictive_kp` (0-200, step 1), `predictive_ki` (0-1, step 0.005) and `predictive_initial_k_loss` (0-10, step 0.1).
- **Outdoor sensor:** `outside_sensor` is required when `heater_control_mode == predictive`. The schema validator raises `errors["base"] = "predictive_requires_outside_sensor"`. The selector already exists (`schemas.py:1153`, a sensor-domain `get_entity_selector`). Add the dependency rows to `tools/config_validator.py:66-69` and `tools/focused_config_dependencies.json:107-111`, and extend the existing `pwm_cycle_duration` row (`required_value: ["tpi", "ai"]`) with `"predictive"`.
- **YAML schema:** in `climate.py:271-278` add the same keys, with `vol.In` for the gate and `vol.Coerce(float/int)` for the rest.
- **Translations:** `translations/en.json` has the heater-control labels and descriptions in two sections (`:88-104` and `:202-220`). Add the new keys to both, plus the options-flow section if one exists.
  - Seen in passing: `ai_initial_heating_power` occurs only in those two sections. Whether the options flow shows untranslated labels for the AI/TPI fields must be checked in the Task 4 build, and fixed there if so.
  - `sk.json` gets the keys in English (the existing practice for new keys: an assumption, to check against git history in Task 4).
  - The new service goes into `services.yaml`.

### 7. Tests

Pure tests port the v5 suites one to one; they use pytest functions with no hass, and live in `tests/unit/`:
- `tests/unit/test_predictive_model.py` gets every test in `.tests/test_model.py` plus the pure ones in `.tests/test_v5_review.py`:
  - `test_covariance_is_symmetric_and_bounded`;
  - `test_blocks_round_trip_per_candidate`;
  - `test_zero_on_rejected_when_below_target_without_reaching_it`;
  - `test_underestimating_model_forces_cold_guard`;
  - `test_overshoot_cost_weight_is_three`;
  - `test_first_thirty_minutes_have_weight_point_two`;
  - `test_planner_ties_choose_smallest_and_candidates_respect_minimum`;
  - `test_tracker_seeds_ewmas_and_counts_nonoverlapping_windows`;
  - `test_tracker_uses_room_counter_at_half_minute_jitter`.

  Plus new: `test_planner_candidates_respect_min_off` (section 1). `test_rls_recovers_known_params` and `test_room_model_picks_true_tau` use **21-day** histories: with the covariance cap, identification needs 21 days (`.tests/test_model.py:37-38, 66-67` at `c761462`).
- `tests/unit/test_predictive_controller.py` gets the gate, floor and persistence tests from `.tests/test_v5_review.py` and `.tests/test_app.py`, retargeted at `PredictiveController`:
  - `test_cold_floor_forces_cycle_and_demotes_for_24h`;
  - `test_cold_floor_still_yields_to_demand_fault_and_exercise`;
  - `test_forced_on_cannot_undo_cold_floor_demotion_after_reload`;
  - `test_gate_requires_skill_against_persistence`;
  - `test_gate_requires_independent_windows_and_seven_days`;
  - `test_gate_checks_alpha_uncertainty`;
  - `test_gate_absolute_mae_from_known_forecast_error`;
  - `test_gate_requires_validations_and_accuracy`;
  - `test_restart_ages_slab_and_first_step_uses_elapsed_time`;
  - `test_round_trip`;
  - `test_appdaemon_v5_room_imports`, which uses a fixture copied from a real v5 state file's room;
  - `test_v4_file_refused`;
  - `test_predictive_freezes_unsaturated_integral`;
  - `test_predictive_loss_update_transfers_integral`;
  - `test_ac_cooldown_excludes_model_and_loss_learner`;
  - `test_legacy_scalar_blocks_and_unpaired_accuracy_migrate`;
  - `test_predictive_without_outdoor_uses_fallback`.
- `tests/unit/test_predictive_closed_loop.py` ports `.tests/test_closed_loop.py`, driving `PredictiveController` plus `plan_pwm` minute by minute against the same plant (`plant_step` from `.tests/test_model.py`, readings rounded to 0.1 °C):
  - `test_predictive_closed_loop_limits`: overshoot ≤ 0.3, MAE over the last 24 h ≤ 0.2, max hot run < 240;
  - `test_predictive_tracks_better_than_pi`: predictive MAE over the last 24 h < PI's, and max overshoot ≤ 0.3. This replaces v5's strict "beats PI on overshoot" expected failure, as the v5 fix round did (heating session, 2026-10-17: PI never overshoots that simulated plant);
  - The solar-mismatch fixture, shared by the two solar tests below, as in v5's final test (`.tests/test_closed_loop.py` at `c761462`):
    - 10 days; theta (0.03, 0.0015, 0), tau 120;
    - outdoor 8 + 5·sin(2πt/1440);
    - solar gain 0.01·sin(π(minute_of_day − 600)/360) from 10:00 to 16:00;
    - noise σ 0.05, seed 42, readings rounded to 0.1 °C; start T 21, S 0.8;
    - feasibility guard: assert alpha/beta + min outdoor ≥ sp + 1.
    - The acceptance is **relative**, as the owner ruled for v5: auto minutes below sp − 0.3 and below sp − 0.7 are each ≤ PI's + 60; auto min T ≥ PI's − 0.1; auto overshoot ≤ PI's + 0.3.
    - Absolute cold time under strong unmodelled solar gain is an open problem shared by PI and predictive (results file, "Open"). It is not a requirement here.
  - **Two solar tests, both must pass, with the same unchanged relative margin** (owner, 2026-10-17, "Decision log"):
    - `test_predictive_auto_not_colder_than_pi_with_solar_mismatch_gated` is the **v5 equivalence test**. Demand is gated with v5's hysteresis on the reading: on below 20.7, off at ≥ 21 (`test_closed_loop.py:48-52` at `c761462`). In the port that gating is driven by switching the setpoint input between 21 and None.
    - `test_predictive_auto_not_colder_than_pi_with_solar_mismatch` is the **acceptance**. Demand is always on, which is the faithful setup for the port: there is no demand switch, and predictive and its PI fallback heat continuously while the hvac mode is HEAT.
  - **The acceptance is expected to fail at first.** v5's own always-on diagnostic (results file, second solar table) has auto over the margin: 2228 vs PI's 2124 + 60 minutes below sp − 0.7, and 4080 vs 3999 + 60 below sp − 0.3. Min T and overshoot pass. Those numbers come from the results file; I did not run them.
    - Task 2b finds and fixes the cause.
    - There is no widened margin, no expected-failure marker and no skip. Until Task 2b lands, the acceptance test fails and the suite's exit code is not 0, so nothing merges past Wave 1's branch.

HA-level tests use the real `hass` fixture and freezer, following `tests/features/test_ai_heating.py`; they go in `tests/features/test_predictive_heating.py`:
- `test_predictive_mode_creates_device`;
- `test_minute_tick_trains_and_publishes_sensors`, which checks the 15 entities, their unique ids, units and state classes;
- `test_heat_cool_early_start_inside_band`: heater_cooler in heat_cool, T inside the band, and a forecast that falls below low gives on > 0. This fails without the change at `heater_cooler_device.py:178`;
- `test_opening_invalidates_and_stops`;
- `test_valve_maintenance_invalidates`;
- `test_ac_active_invalidates_learning`, with the cooler as a wrapped climate in `cool`;
- `test_setpoint_change_restarts_cycle`;
- `test_cold_floor_restarts_cycle_full_on`;
- `test_store_round_trip_across_reload`: unload and reload the config entry; no leaked timer (counts switch calls after unload, as `test_ai_heater_cooler_stops_pwm_when_entity_removed`); slab decayed;
- `test_import_predictive_state_service`, which also refuses a disallowed path;
- `test_entity_removed_stops_minute_tick`.

Config-flow tests (real `hass`) go into the consolidated files the CLAUDE.md names:
- `tests/config_flow/test_simple_heater_features_integration.py` and `test_heater_cooler_features_integration.py`: select predictive, error without an outdoor sensor, persistence through the options flow;
- `test_e2e_simple_heater_persistence.py` and `test_e2e_heater_cooler_persistence.py`: the new keys survive config → options;
- `tests/unit/test_schema_utils.py:377-390`: the new fields.

**How they run here:** the Windows venv used for `fix/ai-heats-above-target`:
- uv Python 3.14 at `%TEMP%/claude/dst-venv`;
- `pytest-homeassistant-custom-component==0.13.324` with the override `lru-dict>=1.4`;
- stub `fcntl.py` and `resource.py` in site-packages;
- `-p winshim` from `%TEMP%/claude/dst-shim` to disable pytest-socket.

Command: `PYTHONPATH=%TEMP%/claude/dst-shim dst-venv/Scripts/python -m pytest -p winshim -q -p no:cacheprovider`, gated on the exit code. The baseline on `bfa605a` was 1649 passed, 2 skipped, exit 0.

**Before release, Docker is still required:** `./scripts/docker-test` (Linux, Python 3.13 per CLAUDE.md) and `./scripts/docker-lint`. isort could not run here: the repo's `setup.cfg` `not_skip` is rejected by current isort. Docker is not installed on this PC.

### 8. Cut-over and rollback, per thermostat

Today, each of the 7 thermostats is a bang_bang heater on `switch.pid_<room>_demand`, and AppDaemon times the real valve. The real valve ids are taken from `apps.yaml` (the parent's "switch.<room>_heating" is not exact for two rooms):

| Thermostat | Type | Valve after cut-over | AppDaemon room |
| --- | --- | --- | --- |
| climate.bathroom_dual | simple heater | switch.upstairs_bathroom_heating | bathroom |
| climate.shower_dual | simple heater | switch.small_bathroom_heating | shower |
| climate.hallway_dual | simple heater | switch.hallway_heating | hallway |
| climate.office_combined_ac | heater_cooler | switch.office_heating | office |
| climate.bedroom_combined_ac | heater_cooler | switch.bedroom_heating | bedroom |
| climate.livingroom_combined_ac | heater_cooler | switch.livingroom_heating | living_room |
| climate.guest_combined_ac | heater_cooler | switch.guest_heating | guest |

**Once, before any room:**
1. Release the fork version with this mode, plus `fix/ai-heats-above-target`.
2. Install it and **restart HA core**. The restart also kills any AI PWM loops leaked before `bfa605a`.
3. Copy the AppDaemon state file (`/addon_configs/a0d7b954_appdaemon/apps/adaptive_pid_state.json`) to `/config/predictive_import/adaptive_pid_state.json` over Samba, right after AppDaemon's 5-minute save.

**Per room. The pilot is the bathroom** (owner, 2026-10-17): `climate.bathroom_dual`, a simple heater, valve `switch.upstairs_bathroom_heating`, AppDaemon room `bathroom`. The other six follow only after the pilot is verified.
1. **Take the room out of AppDaemon.**
   - Remove `rooms.<room>` from `apps.yaml` in the AppDaemon repo, push, and pull in the deployed folder. This is the heating session's deploy path (memory `ha-heating-appdaemon`).
   - AppDaemon's `terminate()` closes all enabled valves on the app reload (`adaptive_pid_heating.py:502-513`). The other rooms re-initialise from the state file.
2. **Reconfigure the thermostat** through its reconfigure flow:
   - heater = the valve in the table;
   - `heater_control_mode` = predictive, `pwm_cycle_duration` 1800 s, `min_cycle_duration` 600 s;
   - `outside_sensor` = `sensor.pirateweather_temperature` (AppDaemon's `outdoor_temperature_sensor`);
   - `predictive_gate` = auto; `predictive_kp`/`predictive_ki` from `apps.yaml` (bathroom 65/0.05);
   - valve maintenance on.

   The reconfigure features step pre-ticks configured features, and unticking deletes them (memory). Keep openings and presets ticked.
3. **Import:** call `dual_smart_thermostat.import_predictive_state` with `entity_id`, `path: /config/predictive_import/adaptive_pid_state.json` and `room`.
4. **Verify:**
   - the 15 sensors exist;
   - `learning_blocks` and `validations_120_independent` equal the AppDaemon values;
   - `mode` matches;
   - the valve is switched only by user-less `homeassistant.turn_on/off` calls on the thermostat's 30-min grid, with no second grid (logbook, as in the 2026-10-16 investigation).
5. **Disable the demand switch** `switch.pid_<room>_demand`, if it is a helper. Nothing reads it any more.

**Rollback per room:**
1. Reconfigure the heater back to `switch.pid_<room>_demand` with `heater_control_mode` bang_bang.
2. Re-add the room to `apps.yaml` and deploy AppDaemon.
3. AppDaemon resumes from its own state file. Its model is stale by the time the thermostat ran, which is acceptable: the slab decays on restore. The thermostat's Store stays on disk, and is ignored until predictive is selected again.

## Tasks (build waves)

- **Wave 1, one Codex builder (pure, add-only):**
  - Task 1 `control/predictive_model.py` + `tests/unit/test_predictive_model.py`;
  - Task 2 `control/pi_fallback.py` + `control/predictive.py` + `tests/unit/test_predictive_controller.py` + `tests/unit/test_predictive_closed_loop.py`.

  Each test is seen failing first, then the suite runs with the exit code read. Wave 1 reports DONE when everything passes except `test_predictive_auto_not_colder_than_pi_with_solar_mismatch` (the always-on acceptance), whose failing numbers it reports.
- **Task 2b: cause of predictive's extra cold time with demand always on** (owner, 2026-10-17). This is a debug of an unknown cause, so it is Opus at effort xhigh (account rule), on the Wave 1 branch.
  - **Method:** run the always-on solar fixture (section 7), and change **one candidate per run**. Record for each run, PI and auto side by side:
    - minutes below sp − 0.3 and below sp − 0.7;
    - min T and overshoot;
    - promotion and demotion minutes, and the gate's `mae120` and `persistence_mae120`.

    No cause is assumed, and no candidate's fix is kept unless its run shows the acceptance margin met.
  - **Candidate 1, outdoor held constant over the horizon** (owner's first candidate):
    - `forecast`/`plan_cycle` hold `T_out` constant over 240 min (`adaptive_pid_heating.py:252-259, 272-293`), while the fixture swings 10 °C a day.
    - First measure the **ceiling**. Give `forecast` and `plan_cycle` an optional per-minute outdoor sequence, and feed it the fixture's **true** future outdoor. This is a test-only oracle.
    - The tracker also re-simulates with the snapshot's constant outdoor (`:328`). So run the oracle twice, planner only and planner + tracker, and report both. The gate's MAE, and so its promotion and demotion timing, may move independently of the planner.
    - If the ceiling meets the margin, the real fix is to feed the HA weather forecast's hourly temperature into the forecast. That means:
      - a `weather` entity selector in the flows (real selector, section 6);
      - `weather.get_forecasts` (type hourly), read at each cycle boundary and interpolated to minutes;
      - falling back to the constant last-known outdoor when no forecast is available.

      The fix is then measured again with a forecast that deviates from the truth by the fixture's own noise model **(mine: how to test the realistic case; the owner names the fix, not the noise)**.
  - **Candidate 2, heat-loss and hold-duty learning under disturbance:** whether `LossLearner` windows ever complete in the fixture (`:78-131`; its 180-min window needs |sp − T| ≤ 0.3 and ≤ 0.2 °C drift), and what `hold_duty` (`:262-264`) returns against the true steady duty.
  - **Candidate 3, PI gains:** kp 65 and ki 0.05 against the fixture's plant.
  - **Stop when the acceptance passes.** Then:
    - write each run's numbers into a results file next to this plan, `docs/plans/2026-10-17-predictive-heater-mode-results.md`;
    - send the cause and the fix to the heating session, since v5 in AppDaemon has the same logic (owner rule: hand defects to their owner);
    - if no candidate closes the gap, report the measured table to the owner instead of changing the margin.
- **Wave 2, one builder (HA glue), stacked on Wave 1 once it reports DONE (it does not wait for Task 2b; if Task 2b's fix adds the weather forecast, its HA part is added to Task 3 or a follow-up of the same builder):**
  - Task 3: `const.py` enum and keys, `hvac_device/predictive_heater_device.py`, `pwm_controller.async_restart`, factory branch, `heater_cooler_device.py` change (section 2), maintenance hook, Store, import service + `services.yaml`, `sensor.py` predictive sensors, reset service;
  - plus `tests/features/test_predictive_heating.py`.
- **Wave 2, a second builder in parallel (flows only; they share no files with Task 3 apart from `const.py`, whose keys Task 3 adds first):**
  - Task 4: `schemas.py`, the YAML schema in `climate.py:271-278` (coordinated: Task 3 does not touch those lines), translations, tools validator/deps/docs;
  - plus the config-flow tests.
- **One Sonnet review of both waves and Task 2b.** Fix rounds for medium and high findings go to the warm builders. Then the full suite on the merged tip (Windows venv, exit code 0, with **both** solar tests passing), then Docker test and lint before release.
- **Task 5, owner and heating session:** release, cut-over of the pilot room (bathroom), then the others (section 8).

## Decision log

- 2026-10-17 (owner, relayed by the heating session):
  - The fallback is a port of v4's PI with heat-loss feed-forward.
  - No heat-then-cool interlock.
  - The pilot room is the bathroom (`climate.bathroom_dual`).
  - The strict "predictive beats PI on overshoot" criterion is replaced by the v5 fix round's "MAE last 24 h < PI's and overshoot ≤ 0.3".
  - v5's solar acceptance is relative to PI (owner ruling recorded in the v5 results file); absolute solar cold time is an open problem shared with PI.
- 2026-10-17 (owner, relayed by the heating session; option "d", none of the offered a/b/c):
  - The **always-on** solar fixture is the must-pass acceptance for the port, with v5's unchanged relative margin: auto minutes below sp − 0.3 and below sp − 0.7 are each ≤ PI's + 60; min T ≥ PI's − 0.1; overshoot ≤ PI's + 0.3.
  - The gated fixture stays as the v5 equivalence test.
  - Before the acceptance can pass, find and fix the cause of predictive's extra cold time in the always-on case (Task 2b):
    - measure the candidates one at a time with the closed-loop fixture, none assumed;
    - first candidate: the constant-outdoor horizon, its ceiling measured with the fixture's true future outdoor;
    - then heat-loss and hold-duty learning under disturbance, then PI gains.
  - No widened margins and no expected failures.

## Ideas (later, owner's choice)

- **Heat-then-cool interlock** (owner said no on 2026-10-17). In heat_cool the cooler would start only once the slab has decayed (S < 0.2), so the AC does not cool against stored floor heat.

## Left out

- **Pre-heat from a preset schedule, neighbouring-room transfer, solar regressor:** these are v5's "Ideas (later)", and none was asked for here.
- **Changing AI or TPI behaviour in heat_cool:** only predictive gets the `heater_cooler_device.py` change. The AI/TPI inconsistency goes to the ledger as an item.
- **Live HA work of any kind:** this plan changes nothing in Home Assistant.
