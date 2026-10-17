# Predictive heater mode: Task 2b results (always-on solar cold time)

Task 2b of `2026-10-17-predictive-heater-mode.md`: find the cause of predictive's extra cold time in
the always-on solar acceptance, `test_predictive_auto_not_colder_than_pi_with_solar_mismatch`, with
the fixture and the margin unchanged.

## Outcome

> **Superseded by the owner's decisions of 2026-10-17.** The gate's persistence demotion is
> dropped, the acceptance is now the mean over seeds 0-31, and plan_pwm's float collapse is fixed.
> The acceptance passes. See "Owner decisions and the runs after them" at the end. This section
> records the state before those decisions: v5's gate and the old plan_pwm comparison. The tool
> now reproduces that baseline as `x_pre_fix_pwm+v5gate` (3676 / 1307). The other earlier
> variants, run today with `+v5gate`, include the plan_pwm fix. The `+v5` rows reproduce
> unchanged, since min off 0 is untouched by that fix. The earlier "exact PWM" rows are the fix
> itself.

- **The margin is not met by any change that keeps the binding constraints.** No production code is
  changed. The acceptance still fails by 12 minutes (1307 vs 1295 minutes below 20.3).
- **Measured cause:** the gate's demotion clause `mae120 > persistence_mae120`.
  - It demotes a predictive controller that is accurate in absolute terms. Over 256 seeds, 512 of
    526 demotions came from that clause alone, at mae120 0.142 (mae_on is 0.3).
  - The persistence baseline is computed from the room the controller under test is holding. It
    falls from 0.370 at promotions (PI in control) to 0.138 at demotions (predictive in control).
  - The demotions land at 10.1 h ± 1.6 h of day, when the unmodelled solar gain starts. So every
    day the solar period and the cold evening go back to the PI fallback.
  - That PI resumes with the integral frozen at promotion. A test-only transplant of the PI-only
    run's integral removes the systematic post-demotion excess (see "Handover").
- **Removing the clause meets the margin** at seed 42 (981 vs 1295 allowed) and at 247 of 256
  seeds. But it changes v5's gate, and so it fails three tests that pin v5's behaviour:
  - the two v5-equivalence tests in `tests/unit/test_predictive_closed_loop.py`;
  - `test_gate_requires_skill_against_persistence`.

  The brief requires those three to keep passing, so the change is not kept. This is an owner
  decision (see "Decision needed").
- **At seed 42 the excess is within the fixture's noise.** I reran the fixture's noise with seeds
  0-255 as a diagnostic; the acceptance keeps seed 42.
  - Auto minus PI, minutes below 20.3: mean +15.1, standard error 11.3. That is a per-seed
    standard deviation of about 180 minutes, against a 60-minute margin.
  - The unchanged baseline meets the margin at 136 of 256 seeds.

## Method

- `tools/predictive_coldtime.py` replicates `simulate("off"|"auto", solar=True)` from the test:
  - 10 days, plant theta (0.03, 0.0015, 0), tau 120;
  - outdoor 8 + 5·sin, solar gain 10:00-16:00, noise σ 0.05, seed 42, readings rounded to 0.1;
  - demand always on, `PredictiveSettings(30, 10)`, the production actuator (min_on = min_off = 10).

  Its baseline reproduces the test's numbers exactly. With `+v5` (min_off 0) it reproduces v5's
  published always-on diagnostic exactly: PI 2124/3999, auto 2228/4080.
- **One change per run.** Each variant patches the pure code in that process only. The oracles are
  test-only.
- **Every run prints:**
  - PI and auto side by side: minutes below 20.7 (sp − 0.3) and below 20.3 (sp − 0.7), min T and
    overshoot;
  - auto's transitions, with mae120 and persistence_mae120 at each one;
  - the minutes below each limit split by auto's gate segments.
- **The seed sweep** reruns the identical fixture with seeds 0-255 and reports auto − PI as a mean
  with its standard error. It also splits that difference into auto's predictive spans and its
  fallback spans after the first promotion.
- **Margin:** auto below 20.7 and below 20.3 each ≤ PI + 60; auto min T ≥ PI − 0.1; auto
  overshoot ≤ PI + 0.3.

## Baseline (production actuator, d2e8cda)

| | PI | auto | allowed |
|---|---:|---:|---:|
| minutes below 20.7 | 3628 | 3676 | 3688 |
| minutes below 20.3 | 1235 | 1307 | 1295 |
| min T | 19.881034 | 19.881034 | ≥ 19.781034 |
| overshoot | 0.577199 | 0.577199 | ≤ 0.877199 |

| minute | auto switches to | mae120 | persistence_mae120 |
|---:|---|---:|---:|
| 10080 | predictive | 0.1971 | 0.4241 |
| 10710 | fallback | 0.1239 | 0.1178 |
| 12765 | predictive | 0.2485 | 0.3591 |
| 13530 | fallback | 0.1140 | 0.1117 |

Minutes below each limit by auto's segments (the runs are identical before minute 10080):

| span | auto mode | PI < 20.7 | auto < 20.7 | PI < 20.3 | auto < 20.3 |
|---|---|---:|---:|---:|---:|
| 0-10080 | fallback | 2715 | 2715 | 981 | 981 |
| 10080-10710 | predictive | 0 | 0 | 0 | 0 |
| 10710-12765 | fallback | 572 | 630 | 155 | 326 |
| 12765-13530 | predictive | 33 | 42 | 0 | 0 |
| 13530-14400 | fallback | 308 | 289 | 99 | 0 |

All of the excess is in the fallback span after the first demotion. The predictive spans have no
minute below 20.3.

## Candidate 1: constant outdoor over the horizon

What changed: `forecast` and `hold_duty` accept a per-minute outdoor tuple, inside the tool only.
The planner, and in the second run also the tracker's re-simulation, get the fixture's true future
outdoor. The hold duty uses element 0, the outdoor the planner holds constant today.

| run (seed 42) | auto < 20.7 | auto < 20.3 | auto min T | auto overshoot | met | transitions (minute: mode mae120/persistence) |
|---|---:|---:|---:|---:|---|---|
| baseline | 3676 | 1307 | 19.881034 | 0.577199 | no | see above |
| planner oracle | 3960 | 1619 | 19.881034 | 0.636820 | no | 10080 P .1971/.4241; 10905 F .2438/.2352; 12810 P .2517/.3613; 13785 F .2357/.2286 |
| planner + tracker oracle | 3785 | 1485 | 19.881034 | 0.636820 | no | 10080 P .1729/.4241; 10875 F .2303/.2282; 14265 P .2483/.3579 |

PI is 3628 / 1235 / 19.881034 / 0.577199 in every candidate 1 run.

- The ceiling does not meet the margin. Over 256 seeds it is worse than the baseline (table "Seed
  sweep"):
  - the predictive spans gain 48 (planner) and 64 (planner + tracker) minutes below 20.7;
  - overshoot rises by 0.11.
- Why the true outdoor makes the planner colder: I don't know.
- The ceiling failed, so the pure sequence API was not kept in `predictive_model.py`, and the
  realistic-forecast run was not made. The HA weather feed is not needed for this fixture.

## Candidate 2: heat-loss and hold-duty learning under disturbance

**Measured facts (seed 42):**
- **LossLearner windows complete.**
  - PI run: 17 windows; k goes from 2.0 to 5.007. The plant's no-solar value is
    100·β/α = 5.000 %/K.
  - Auto run: 18 windows, 6 of them during predictive; k ends at 4.661.
  - Resets: PI 537 "outside band" and 13 "room drift or small delta T"; auto 504 and 13.
- **hold_duty against the plant's steady duty.**
  - The active model at outdoor 8 °C gives 64.3 % on day 1 and 60.0 % on day 9.
  - The plant needs 65 % without solar. With the daily-mean solar gain removed (5.31 % duty), it
    needs 59.7 %.
  - The learned theta (0.0206, 0.00097, 0.0002) is biased against the plant's (0.03, 0.0015, 0).
    The ratio β/α, which sets the hold duty, is close.

| run (seed 42) | what changed | PI < 20.7 | auto < 20.7 | PI < 20.3 | auto < 20.3 | PI / auto min T | PI / auto overshoot | met |
|---|---|---:|---:|---:|---:|---|---|---|
| hold oracle | `hold_duty` returns the plant's no-solar steady duty β(sp − T_out)/α | 3628 | 3698 | 1235 | 1309 | 19.881034 / 19.881034 | 0.577199 / 0.577199 | no |
| k oracle | k_loss = 5.0, and no loss window ever completes (PI and auto) | 3308 | 3104 | 1023 | 745 | 20.172203 / 20.182142 | 0.667009 / 0.667009 | yes |

Transitions:
- hold oracle: 10080 P .1971/.4241; 10710 F .1276/.1207; 12765 P .2391/.3496; 13545 F .1021/.0958;
  14295 P .2446/.3559.
- k oracle: 10080 P .2144/.4649; 10710 F .1289/.1212; 12780 P .2357/.3433; 13545 F .1176/.1155.

The k oracle meets the margin at seed 42, but it is not a fix:
- it is an oracle;
- it changes PI's own baseline;
- over 256 seeds it moves auto − PI below 20.3 from +15.1 to +24.6 (± 10.4), which is no better.

## Candidate 3: PI gains (kp 65, ki 0.05)

What changed: one gain, for PI and auto alike.

| run (seed 42) | PI < 20.7 | auto < 20.7 | PI < 20.3 | auto < 20.3 | PI / auto min T | PI / auto overshoot | met |
|---|---:|---:|---:|---:|---|---|---|
| kp 40 | 3937 | 3872 | 1202 | 1316 | 19.803584 / 19.803584 | 0.626533 / 0.602425 | no |
| kp 100 | 3450 | 3488 | 1233 | 1328 | 19.980238 / 19.980238 | 0.471550 / 0.471550 | no |
| ki 0.025 | 3774 | 3734 | 1449 | 1436 | 19.818641 / 19.818641 | 0.500703 / 0.443900 | yes |
| ki 0.1 | 3698 | 3509 | 1766 | 1677 | 19.962741 / 19.962741 | 0.607145 / 0.519767 | yes |

Transitions:
- kp 40: 10080 P .2146/.3935; 10725 F .1278/.1264; 12825 P .2554/.3747; 13530 F .1119/.1092;
  14265 P .2350/.3402.
- kp 100: 10080 P .1709/.3495; 10650 F .0969/.0930.
- ki 0.025: 10080 P .1813/.3253; 10635 F .1016/.1008; 11355 P .2036/.3007; 12060 F .1276/.1223;
  14265 P .2546/.3729.
- ki 0.1: 10080 P .2052/.4161; 10680 F .1223/.1143; 12840 P .2276/.3412; 13545 F .1128/.1099.

ki 0.025 and ki 0.1 both meet the margin at seed 42. Over 256 seeds they move auto − PI below 20.3
in opposite directions: −21.1 ± 11.4 and +46.4 ± 10.9. So passing at seed 42 is not evidence of a
cause. The defaults are also not what the live rooms use: the cut-over configures per-room gains.
Not kept.

## Other candidates found on the way

| run (seed 42) | what changed | PI < 20.3 | auto < 20.7 | auto < 20.3 | met | transitions |
|---|---|---:|---:|---:|---|---|
| exact PWM | plan_pwm rounded to µs, so a planned 20/30 stays 20 instead of running 30 (defect 1 below) | 1235 | 3743 | 1318 | no | 10080 P; 10695 F .1243/.1212; 14295 P .2609/.3835 |
| bank charges full cycle | DutyBank deducts the 30 min the actuator runs when the banked pulse exceeds cycle − min_off (defect 2 below) | 1619 | 3830 | 1496 | yes | 10080 P .1861/.3405; 10680 F .1167/.1165; 14265 P .2513/.3656 |
| bank + exact PWM | both of the above | 1619 | 3861 | 1563 | yes | 10080 P; 10680 F .1233/.1181; 12810 P .2565/.3815; 13590 F .1404/.1333 |
| bumpless demotion | at demotion the integral is back-calculated so PI output equals the duty predictive was running | 1235 | 4100 | 1424 | no | 10080 P; 10710 F .1239/.1178; 14310 P .2561/.3840 |
| integrate while predictive | the PI integral keeps integrating its error while predictive runs | 1235 | 3676 | 1416 | no | 10080 P; 10710 F; 12825 P .2318/.3441; 13500 F .1028/.0979 |
| integral transplant (oracle) | at demotion the integral is set to the PI-only run's integral at that minute | 1235 | 3705 | 1338 | no | 10080 P; 10710 F; 11385 P .2191/.3193; 12120 F .1346/.1262; 14280 P .2253/.3289 |
| **demote on absolute error only** | the `mae120 > persistence_mae120` demotion clause is removed; demotion only at mae120 > 0.45 (and the cold floor) | 1235 | **2787** | **981** | **yes** | 10080 P .1971/.4241, no demotion |

In these runs PI below 20.7 is 3628, except 3899 in both bank runs. Min T is unchanged from the
baseline in every row, PI's in the bank runs being 19.745290. Overshoot stays within 0.3 of PI's in
every row.

The two bank runs meet the margin at seed 42 by making PI colder (1619 minutes below 20.3, from
1235). Over 256 seeds the bank change leaves auto systematically colder than PI: +29.7 ± 6.6 below
20.3, and PI's mean rises from 1374 to 2005 minutes. Not kept.

## Seed sweep (diagnostic, seeds 0-255, the fixture otherwise unchanged)

Auto − PI, mean (standard error). "Spans" are auto's segments after the first promotion.

**Production actuator (min_off 10):**

| variant | < 20.7 | < 20.3 | predictive spans < 20.7 / < 20.3 | fallback spans < 20.7 / < 20.3 | seeds meeting margin |
|---|---:|---:|---|---|---:|
| baseline | −25.4 (6.1) | +15.1 (11.3) | −19.0 / −0.0 | −6.4 / +15.1 | 136 |
| c1 planner oracle | +77.4 (7.9) | +46.6 (11.1) | +48.2 / −0.1 | +29.2 / +46.7 | 79 |
| c1 planner + tracker oracle | +103.9 (8.1) | +50.5 (10.7) | +64.2 / −0.0 | +39.7 / +50.6 | 78 |
| c2 hold oracle | −26.8 (5.9) | +8.6 (10.8) | −17.9 / −0.0 | −8.9 / +8.6 | 152 |
| c2 k oracle | +0.0 (6.7) | +24.6 (10.4) | −16.8 / −0.1 | +16.8 / +24.7 | 134 |
| c3 kp 40 | −121.5 (9.1) | −11.8 (8.0) | −65.7 / −0.0 | −55.9 / −11.8 | 172 |
| c3 kp 100 | +19.4 (6.5) | +34.7 (12.7) | −3.8 / −0.2 | +23.2 / +34.9 | 128 |
| c3 ki 0.025 | −51.3 (6.6) | −21.1 (11.4) | −23.5 / −0.0 | −27.8 / −21.1 | 159 |
| c3 ki 0.1 | +11.2 (6.0) | +46.4 (10.9) | −14.6 / −0.0 | +25.8 / +46.4 | 121 |
| exact PWM | −28.8 (6.1) | +11.2 (10.9) | −19.2 / −0.0 | −9.6 / +11.3 | 152 |
| bank charges full cycle | +14.6 (3.5) | +29.7 (6.6) | −9.7 / −0.0 | +24.3 / +29.7 | 155 |
| bank + exact PWM | +21.2 (4.2) | +42.0 (6.6) | −8.6 / −0.0 | +29.7 / +42.0 | 129 |
| bumpless demotion | +75.8 (22.9) | +39.8 (15.2) | −39.9 / −8.4 | +115.7 / +48.1 | 75 |
| integrate while predictive | −23.6 (6.0) | +17.0 (11.1) | −18.3 / −0.1 | −5.3 / +17.1 | 147 |
| integral transplant (oracle) | −37.2 (6.4) | −16.5 (11.4) | −19.7 / −0.1 | −17.4 / −16.4 | 160 |
| demote on absolute error only | −532.7 (17.1) | −250.5 (9.9) | −497.1 / −187.5 | −35.6 / −63.0 | 247 |

**v5's actuator (min_off 0; less noisy, so small effects show):**

| variant | < 20.7 | < 20.3 | predictive spans < 20.7 / < 20.3 | fallback spans < 20.7 / < 20.3 | seeds meeting margin |
|---|---:|---:|---|---|---:|
| baseline | +15.0 (3.4) | +23.3 (2.9) | −9.4 / +0.0 | +24.5 / +23.3 | 183 |
| c1 planner oracle | +84.9 (6.0) | +18.9 (3.1) | +42.3 / +0.0 | +42.6 / +18.9 | 111 |
| c1 planner + tracker oracle | +131.9 (6.3) | +31.6 (3.0) | +62.4 / +0.0 | +69.5 / +31.6 | 58 |
| c2 hold oracle | +16.1 (3.5) | +20.7 (3.0) | −7.4 / +0.0 | +23.6 / +20.7 | 168 |
| c2 k oracle | −24.3 (4.3) | +25.6 (2.9) | −27.5 / +0.0 | +3.1 / +25.6 | 190 |
| bumpless demotion | +443.0 (25.3) | +23.6 (11.0) | −18.1 / −11.4 | +461.1 / +34.9 | 19 |
| integrate while predictive | +17.8 (3.4) | +21.3 (2.9) | −8.8 / +0.0 | +26.5 / +21.3 | 173 |
| integral transplant (oracle) | −10.4 (3.2) | +4.9 (3.2) | −9.5 / +0.0 | −0.9 / +4.9 | 213 |
| demote on absolute error only | −526.2 (16.3) | −461.4 (9.7) | −453.9 / −339.0 | −72.3 / −122.5 | 256 |

Seed 42 with v5's actuator: the baseline is PI 2124 / auto 2228 below 20.3 (fails by 44). The
integral transplant gives 2189 (fails by 5). The persistence-clause removal gives 1733 (passes).

## Handover

At each predictive → fallback switch, I compared auto's state with the PI-only run's state at the
same minute, over 256 seeds. Values are means (standard error).

| actuator | switches | room T auto − PI | slab | PI integral | k_loss |
|---|---:|---:|---:|---:|---:|
| production | 526 | +0.127 (0.004) | +0.024 (0.002) | −12.8 (2.4) | −0.004 (0.009) |
| v5 (min_off 0) | 374 | +0.099 (0.004) | +0.015 (0.002) | −27.7 (2.0) | +0.022 (0.008) |

Predictive hands over a warmer room, with an integral frozen at its promotion value that is lower
than the PI-only run's. With v5's actuator, putting the PI-only integral in at the switch (oracle)
moves the post-demotion excess below 20.3 from +23.3 (2.9) to +4.9 (3.2). Neither implementable
handover change helps:
- back-calculating the integral to the running duty makes it much worse (+443 below 20.7);
- integrating while predictive changes nothing (+21.3).

## Decision needed (owner)

- **The fix that meets the margin** is the removal of `or tracker.mae120 > tracker.persistence_mae120`
  from the demotion test, at `control/predictive.py:136`.
  - It fails the two v5-equivalence tests:
    - gated auto becomes 3231/7791 against v5's 3175/7752, with demotion at 10954 instead of 10905;
    - always-on auto becomes 1733/3502 against 2228/4080.
  - It also fails `test_gate_requires_skill_against_persistence`, which demotes at mae120 0.41
    against persistence 0.40.
  - v5 has the same clause, at `adaptive_pid_heating.py:739-740` at `c761462`.
- **Other ways to keep a skill check without the circular baseline** exist, for example a
  persistence demotion only above an absolute error floor. I have not measured any of them.
- **The frozen integral** (v5 `adaptive_pid_heating.py:665-667`; the port's `predictive.py`
  predictive branch of `minute`) is the measured cause of the smaller post-demotion excess. I did
  not find an implementable handover that removes it.

## Defects seen on the way (not fixed here)

1. **A planned 20 of 30 minutes runs 30, with the production minimum off.**
   - `100 · 20 / 30` % of 1800 s is 1200.0000000000002 s on, so the off time is
     599.9999999999998 s, which is < 600.
   - `plan_pwm(66.66666666666667, 1800, 600)` returns full on. The test `Driver` and
     `_sample_loss` apply the same collapse.
   - In the seed-42 auto run, predictive planned 20 four times, and each ran 30.
   - The planner's candidate set `[min_on, cycle − min_off]` is meant to match the actuator (plan
     section 1). This breaks that at the top candidate.
   - v5 is not affected: its actuator is `minute + 0.05 < on_minutes`, with no minimum off.
2. **The PI fallback's DutyBank does not know min_off.**
   - A banked pulse in (cycle − min_off, cycle) runs the full cycle, but only the planned minutes
     are deducted. In the seed-42 PI run, at the minute-10890 boundary, the bank (9.94 carried
     plus 35.6 % of 30) charges 20.62 minutes and the valve runs 30.
   - Charging the full cycle makes PI much colder in this fixture: mean 2005 against 1374 minutes
     below 20.3 over 256 seeds. So which accounting is wanted is a design choice.
   - Port only: v5 has no minimum off.

## Owner decisions and the runs after them (2026-10-17)

**Decisions** (owner, relayed by the coordinator):
1. **Production drops the persistence demotion.** Predictive demotes only when mae120 > mae_off;
   promotion is unchanged.
   - `PredictiveSettings.persistence_demotion` (default False) is internal, not a config option.
   - The two v5-equivalence tests set it True and still match v5's published numbers to 1e-6.
2. **The acceptance is the same fixture and margin, on the mean over seeds 0-31** of the per-seed
   differences auto − PI:
   - the mean difference in minutes below 20.7 and below 20.3 is each ≤ 60;
   - min T: mean difference ≥ −0.1;
   - overshoot: mean difference ≤ 0.3.

   It keeps the production actuator (min_on = min_off = 10). Seed 42 is now printed only.
3. **plan_pwm's float collapse is fixed** at the comparison: a segment is shorter than its minimum
   only by more than 1 µs.
   - Addition (mine): plan_pwm takes an optional separate `min_off_seconds`, so the loss learner
     and the test Driver use its comparison instead of copies of it. Without that, the fixture's
     actuator would have kept the collapse.
4. **The duty bank versus min-off accounting is left as is.**

**Commits on predictive/task2b-coldtime:**
- 45de059: plan() no longer counts a minute or drops a pending cold floor (review HIGH 1 and
  MEDIUM 3, same branch).
- 7c775ee: the gate.
- ae0fd3e: the multi-seed acceptance.
- f05d72c: plan_pwm.

**The 32-seed acceptance, production gate, production actuator** (mean over seeds 0-31; standard
error in brackets; from the test's print, and the tool's sweep for the errors):

| run | PI < 20.7 | auto < 20.7 | auto − PI < 20.7 | PI < 20.3 | auto < 20.3 | auto − PI < 20.3 | auto − PI min T | auto − PI overshoot | seeds meeting the per-seed margin |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| before the plan_pwm fix (`x_pre_fix_pwm`) | 3605.9 | 2999.9 | −606.0 (46.6) | 1411.6 | 1114.1 | −297.5 (29.0) | −0.0021 | −0.0114 | 31/32 (seed 24 fails) |
| after the plan_pwm fix (`baseline`) | 3605.9 | 3095.8 | −510.1 (41.4) | 1411.6 | 1120.6 | −291.0 (25.3) | +0.0000 | −0.0150 | 31/32 (seed 24 fails) |
| for comparison: v5's gate, before the fix (`x_pre_fix_pwm+v5gate`) | 3605.9 | 3596.9 | −9.0 (19.1) | 1411.6 | 1406.3 | −5.2 (28.5) | −0.0013 | −0.0062 | 16/32 |
| for comparison: v5's gate, after the fix (`baseline+v5gate`) | 3605.9 | 3562.7 | −43.2 (16.8) | 1411.6 | 1390.4 | −21.2 (28.6) | −0.0041 | −0.0149 | 20/32 |

Means of PI and auto min T after the fix: 19.913255 and 19.913258. Overshoot: 0.510929 and
0.495889.

Seed 24 after the fix: auto 1251 against PI 1165 minutes below 20.3. With v5's gate the means
also meet the margin, so the multi-seed acceptance alone does not guard the gate. That is
`test_production_gate_never_demotes_on_persistence`'s job.

**Seed 42 (diagnostic only):**

| run | PI < 20.7 / < 20.3 | auto < 20.7 / < 20.3 | auto transitions |
|---|---|---|---|
| production gate, before the plan_pwm fix | 3628 / 1235 | 2787 / 981 | 10080 predictive |
| production gate, after the fix | 3628 / 1235 | 3296 / 1196 | 10080 predictive; 12584 fallback (cold-room floor) |

After the fix, seed 42's predictive span ends at 12584 by the cold-room floor: a single reading
of 20.4. The true temperature was 20.534 then and never below 20.5 in that span.

**What moved with the plan_pwm fix** (all 72 closed-loop runs, before against after):
- **Unchanged (38 runs):** every PI run, and every min-off-0 run. The latter are both
  v5-equivalence tests, PI and auto.
- **Changed:** the production-actuator predictive runs. These are the 33 solar auto runs (seeds
  0-31 and 42) and the non-solar gate-"on" run of `test_predictive_closed_loop_limits` and
  `test_predictive_tracks_better_than_pi`:
  - overshoot 0.0678 → 0.0209;
  - last-day MAE 0.0244 → 0.0377;
  - valve-on minutes 3559 → 3551.

  Both of those tests still pass.
- **Outside the closed loop:** no existing test changed outcome, TPI and AI included. I did
  not compare the internal values of tests that only assert.

**Full suite:** 1720 passed, 2 skipped, exit 0, in 71 s (42.9 s before). The 32-seed acceptance
takes 28.8 s of that.

## Cold-room floor after three consecutive minutes (owner, 2026-10-17)

**Decision:** the floor trips only after 3 consecutive valid minutes with T < sp − 0.5 while
heating is wanted.
- A minute that is not below the threshold resets the count. So does an invalid or faulted minute,
  and (mine) a missed tick, since "consecutive" means a tick every 1 ± 0.05 min.
- A fault still cancels a pending floor.
- A reading seen only by `plan()` is one more sample of the last minute, not a minute: it can trip
  only the one-sample rule.
- `PredictiveSettings.cold_floor_minutes` is 3, an internal field. The v5-equivalence tests set 1
  (v5's rule) and still match v5 to 1e-6.
- Commit 56779dc, on the merge of predictive/task3-glue (fast-forward to 3ae7813).

**32 seeds, production gate and actuator** (mean over seeds 0-31; standard error in brackets):

| run | PI < 20.7 | auto < 20.7 | auto − PI < 20.7 | PI < 20.3 | auto < 20.3 | auto − PI < 20.3 | auto − PI min T | auto − PI overshoot | seeds meeting the per-seed margin | cold-floor demotions |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| before: one-sample floor (`x_one_sample_floor`) | 3605.9 | 3095.8 | −510.1 (41.4) | 1411.6 | 1120.6 | −291.0 (25.3) | +0.0000 | −0.0150 | 31/32 (seed 24 fails) | 22 |
| after: three minutes (`baseline`) | 3605.9 | 3048.7 | −557.2 (36.3) | 1411.6 | 1096.8 | −314.8 (24.9) | +0.0000 | −0.0161 | 31/32 (seed 5 fails) | 13 |

- The margin holds on the means, so the change is kept.
- No demotion in either run came from mae_off; every one is the cold-room floor.
- **The failing seed:**
  - Seed 5 after the change: auto 1423 against PI 1335 minutes below 20.3. It was 1392 before
    (met).
  - Seed 24 after the change: 1073 against 1165 (met).
- **Seed 42 (diagnostic only):**
  - After the change: auto 3069 / 981 against PI 3628 / 1235. Auto stays predictive from minute
    10080 to the end, with no demotion.
  - Before the change: 3296 / 1196, with the floor demotion at 12584.

**Device tests:** three tests in `tests/features/test_predictive_heating.py` (owned by the task3
builder, not changed here) still expect the one-sample floor and fail:
- `test_cold_floor_restarts_cycle_full_on`;
- `test_store_delayed_save_and_immediate_gate_save`;
- `test_cycle_boundary_saves_gate_switch_immediately`.

With `cold_floor_minutes` temporarily set back to 1, all 43 tests in that file pass. With three
ticks of the low reading in each of the three tests, tried locally and reverted, they pass at 3.
The third test then trips on the minute tick, not on the restart.

**Full suite at 56779dc:** 1818 passed, 2 skipped, 3 failed (the three above), exit 1.
