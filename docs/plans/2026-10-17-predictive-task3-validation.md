# Task 3 validation record

Plan: `2026-10-17-predictive-heater-mode.md`, Task 3 only.

- Device creation, setpoint restart and cold-room restart: three tests failed before implementation, then passed.
- Heat-cool early start, opening cooldown, maintenance ownership, sibling AC cooldown and temperature faults: five tests failed before implementation, then all eight passed.
- Merged the pure-controller minimum-off fix. Full suite immediately after merge: 1766 passed, 2 skipped, one failure, exit 1. The failure is `test_predictive_auto_not_colder_than_pi_with_solar_mismatch` (1307 cold minutes versus PI's 1235 + 60).
- Final targeted HA suite: 32 passed, exit 0. Every one of the plan's 11 named tests passes.
- Final full suite: 1795 passed, 2 skipped, 1 failed, 11 warnings, exit 1. The only failure is `tests/unit/test_predictive_closed_loop.py::test_predictive_auto_not_colder_than_pi_with_solar_mismatch`.
- Black check and flake8 with `setup.cfg`: all nine changed Python files pass, exit 0.

Decisions and scope clarifications:

- The initial heat-cool mode leaves child modes unset in existing code. Predictive explicitly synchronizes the child mode at startup and on mode changes, ensuring demand reflects the parent's mode.
- Predictive handles heat-cool safety and selection before the legacy toggle: this cancels its independent PWM timers when cooling wins and keeps floor/opening overrides in the inherited PWM control path.
- A small overridable temperature-readiness hook in `PwmHeaterDevice` preserves its startup behavior and lets predictive use feed-forward after a temperature fault.
- Opening and AC transitions are tracked as events as well as sampled, so activity between minute ticks still invalidates learning.
- Production leaves `PredictiveSettings.min_off_minutes` at its default (equal to minimum on-time).
- The model inputs are converted from HA's temperature unit to Celsius, matching the learned coefficients, fixed thresholds, imported models and diagnostic native units.
- Sensor listeners catch and rate-limit their own write failures as well as the publisher doing so: HA dispatches listeners in separate jobs, outside the sender's exception handler.
- A stopped PWM cycle uses a bounded virtual cycle for forecasts, retaining the pure controller's cached-path behavior during floor overrides.
- The climate removal hook awaits the device's removal-save task, so a config-entry reload cannot read the Store before the previous instance finishes saving.
- YAML now calls the existing flow validator. Previously the YAML schema accepted missing outdoor sensors and timings that cannot match the actuator; no flow implementation was rewritten.
- The device uses a monotonic clock for the controller's elapsed minutes and cycle age. UTC remains the clock for persisted timestamps and cooldown deadlines.
- Additional tests extend the plan's 11 named HA tests: non-heating modes, faults, floor overrides, timing jitter, Store errors and save timing, publisher/listener isolation, Fahrenheit, YAML dependencies, cooler minimum duration and clock adjustments.
- Reset creates a fresh fallback model and immediately replans; an explicitly configured gate of `on` remains authoritative on that replan. Reset does not silently change the configured gate.

Defects observed in passing:

- Existing AI/TPI heat-cool paths can switch the valve off without stopping the PWM timer; excluded by the plan.
- Heat-cool startup does not initialize heater/cooler child modes; corrected for predictive only.
- The post-merge suite emitted 11 unawaited AsyncMock warnings at `options_flow.py:730` in existing options/preset tests.
- Existing multi-device mode propagation can leave a heater in HEAT when the parent changes to a non-heating mode; predictive now synchronizes its child mode explicitly.
- Existing stale-temperature handling only marks an active thermostat as stalled; predictive now marks idle sensors stale too, using feed-forward and excluding the stale sample.
- The first predictive publication implementation let HA listener exceptions escape its rate limiter; fixed with listener-side isolation, after a failing test.
- The first floor-override forecast implementation could simulate an arbitrarily old cycle and re-simulate each minute; fixed with bounded virtual cycles and tested cache reuse.
- The first predictive input adapter passed Fahrenheit to Celsius model coefficients; fixed with native-unit conversion, after a failing test.
- The first Store implementation missed immediate saves for gate changes at a direct PWM boundary; fixed after a failing test.
- YAML accepted invalid predictive dependencies/timing; fixed by sharing the flow validator, after three failing cases.
- The first predictive heat-cool branch bypassed the cooler's minimum duration on a stop; fixed by leaving active cooling decisions with the existing cooler controller, after a failing test.
- The first device clock followed wall-clock adjustments and could produce negative elapsed minutes; fixed with a monotonic clock, after a failing test.
- The known always-on solar acceptance still fails: 1307 minutes below setpoint minus 0.7 versus the allowed 1295; the gated equivalence test passes.

Entity unique IDs are `{sensor_key}_predictive_{key}`, where `sensor_key` is the config entry ID, YAML unique ID or YAML name. The exact 15 keys are:

| Key | Native unit | Device class | State class |
| --- | --- | --- | --- |
| forecast_30 | °C | temperature | measurement |
| forecast_60 | °C | temperature | measurement |
| forecast_120 | °C | temperature | measurement |
| forecast_180 | °C | temperature | measurement |
| forecast_240 | °C | temperature | measurement |
| mae_60 | °C | none | measurement |
| mae_120 | °C | none | measurement |
| persistence_mae_120 | °C | none | measurement |
| mode | none | enum | none |
| alpha_rel_sd | none | none | measurement |
| slab | none | none | measurement |
| tau | min | none | measurement |
| planned_on_minutes | min | none | measurement |
| validations_120_independent | none | none | measurement |
| learning_blocks | none | none | measurement |

The YAML test creates `test_predictive_` followed by each key. Config-entry tests verify the entry ID prefix and registry ownership after reload. The existing action-reason sensor remains separate. The mode sensor carries `demoted_until`; the climate carries `predictive_mode` and the existing duty-cycle attribute.

Pure-controller changes: none authored for Task 3. The supplied minimum-off fix was merged from the pure branch, and production uses its default. No additional pure API changes were needed.

Implementation commits before this final validation commit:

- `ddf86af`: predictive device, factory, immediate PWM restart.
- `a0f217c`: merge of the requested pure fix at `d2e8cda`.
- `5ffec85`: demand, safety overrides, heat-cool dispatch and AC validity.
- `82680a0`: Store, import/reset, sensor entities, publication isolation and tests.

Relayed update handled: the pure minimum-off fix was merged when requested. No unrelated relayed message was received.

Left out as scoped: live HA operations, release/push, the pure solar investigation, and changes to AI/TPI heat-cool behavior. Windows verification was used as requested; Docker verification remains a release prerequisite.

Task 3 review fix round (2026-10-17)

Implemented in `37e3f73`:

- HIGH 2: unselected composite subtrees clear predictive demand, and the predictive mode setter cancels its independent PWM timer. HA tests cover heater + fan, heater + dryer, and heater-cooler + dryer, including 61 minutes without later valve-on calls and a return to heating.
- MEDIUM 4: shared PWM stop checks actual actuator activity before returning early. A cancelled cycle cannot leave a forced-on valve running. Tests cover floor protection followed by OFF in predictive, AI, and TPI, plus cooler selection after cancellation.
- MEDIUM 5: removal closes the valve once, then saves; the existing awaited shutdown makes this apply to reload/unload too. The removal test requires exactly one turn_off and no later calls. Shared PWM catches duty-computation errors, logs them, runs a zero-duty cycle and retries at the next boundary.
- MEDIUM 6: Store load failures warn and start fresh, while still registering minute processing. Tests exercise OSError and UnsupportedStorageVersionError.
- LOW/MEDIUM 8: learning uses is_active, correctly recognizing valve entities reporting open.

Verification:

- Fail-first run: 11 failed, 31 deselected, exit 1; each assertion reproduced the corresponding review defect before implementation. The unsupported-version variant was added afterward to extend read-error coverage.
- Predictive HA file: all 43 cases pass in the final targeted/full runs (32 original cases plus 11 additional parameterized cases).
- Predictive + dual-mode + fan-mode targeted run: 444 passed, exit 0.
- Final full suite: 1806 passed, 2 skipped, 1 failed, 11 warnings, exit 1, 50.53 seconds.
- Exact failure list: tests/unit/test_predictive_closed_loop.py::test_predictive_auto_not_colder_than_pi_with_solar_mismatch.
- The known assertion remains 1307 <= 1235 + 60; no other failure remains.
- uvx black --check and uvx flake8 --config setup.cfg pass on all five changed Python files (exit 0 for each).
- git diff --cached --check passes, and the staged changes were reviewed before committing.

Scope and implementation adjustments:

- The new shared PWM constructor callback reads actuator activity without relying on PWM timer state. It is an HA controller change, not a change under control/.
- OFF propagation is deliberately limited to predictive descendants: a first broad implementation broke 31 legacy heat-cool/fan tests, because those paths retain separate child modes. Narrowing propagation restored every one of those tests without changing their assertions.
- No new entity unique IDs are introduced; the 15 IDs documented above are unchanged.
- No control/*.py or tests/unit/test_predictive_*.py files were edited; no pure API change was requested. No new pure-branch merge instruction arrived during this fix round.
- No push and no live Home Assistant changes were made.

Defects observed in this round, one per line:

- Predictive timers retained heating demand in FAN_ONLY/DRY composites; fixed.
- Shared PWM stop left an externally forced-on valve active after cancellation; fixed for predictive, AI and TPI.
- Predictive removal did not close the valve; fixed.
- A duty-computation exception could stop the PWM chain while leaving the valve on; fixed.
- A Store read/version exception prevented startup from registering minute processing; fixed.
- Valve-domain open states were recorded as off for learning; fixed.
- Broad OFF propagation in the first fix attempt broke legacy heat-cool/fan selection; corrected before the implementation commit.
- Existing options/preset tests still emit 11 unawaited AsyncMock warnings at options_flow.py:730; unchanged.
- The known always-on solar acceptance failure remains assigned to the pure-code builder; unchanged.
