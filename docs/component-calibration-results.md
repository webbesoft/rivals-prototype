# Fixture calibration and history readiness

24 November 2026. `decision-v12-calibrated` is a new shadow revision with a damped
fixture-strength adjustment and fitted midfielder/forward calibration. It preserves
the deployed `heuristic-v1` player formula and the original `decision-v11` comparison.
The local GW12 v4 capture has usable history for all 52 primary candidates. The
revision improves historical error but still does not satisfy promotion criteria.

## Earlier season fitting

The [declared protocol](component-calibration-protocol.json) fits on 2022/23,
selects the fixture-strength setting on 2023/24, and then refits attacker calibration
on both seasons. Neither the inspected 2024/25 diagnostic nor the inspected
2025/26 evaluation supplies fitting or selection labels.

The selected fixture-strength power is **0.25**, from the fixed grid
0, 0.25, 0.5, 0.75 and 1. Normalised team attack and defence strengths are raised to
this power before forming their ratios. A strength ratio of 2 therefore becomes
about 1.189 rather than 2. Venue baselines and all player scoring components remain.
This damps the current proxy's influence; it does not claim to have independently
validated live FPL team strengths against historical rolling-goals proxies.

Midfielder and forward calibration uses:

```text
points = max(0, slope × raw_component_points + offset × expected_appearances)
```

Expected appearances include current availability and provisional double-gameweek
rotation. Injured/non-playing players and blank weeks cannot receive free points
from an unconditional intercept. Calibration is fitted as residual corrections
toward identity, with penalty 100 on RMS-scaled features. GK/DEF retain identity
point calibration. The component breakdown includes a separate calibration term
and still sums to the forecast; its variance proxy is not a calibrated distribution.

| Position | Candidate training observations | Fitted slope | Offset per expected appearance |
| --- | ---: | ---: | ---: |
| MID | 1,540 | 0.9936 | 0.3459 |
| FWD | 476 | 0.9799 | 0.6141 |

The earlier seasons support near-identity slopes and positive offsets. We did not
force the much smaller slopes observed in 2025/26 into a fitted correction.
The [parameter artifact](component-calibration-parameters.json) freezes coefficients,
training seasons, source hashes and protocol hash. The
[selection audit](component-calibration-selection.json) retains every grid setting
and weekly validation error, including the original strength power of 1.

## Inspected diagnostic results

On 34 usable 2024/25 gameweeks, all variants score the same 1,896 candidate
observations: the default and original v11 top-40 union. This is an already-inspected
diagnostic season, not a new untouched holdout.

| Model | Candidate RMSE | Mean realised XI plus captain |
| --- | ---: | ---: |
| Deployed heuristic | 3.975 | 64.68 |
| Original component v11 | 3.780 | 62.50 |
| Revised fixture strength and attacker calibration | 3.730 | 61.21 |
| Revised fixture strength without attacker recalibration | 3.724 | 62.00 |

The full revision's mean weekly RMSE difference against v11 is
**−0.0458 [−0.0871, −0.0101]**. Its deviance difference is
**−0.0713 [−0.1348, −0.0140]**. Intervals use 5,000 paired whole-gameweek bootstrap
draws with seed 1729; they are marginal and do not model temporal dependence.

The attacker correction slightly worsens pooled error against the fixture-only
control on this diagnostic. Keeping that control in prospective captures lets us
measure whether recalibration adds value. Neither revision has demonstrated higher
realised lineup returns: the full revision's XI-plus-captain difference against
v11 is **−1.29 [−3.79, +1.32]** points per week.

| Position | V11 point calibration slope | Revised slope |
| --- | ---: | ---: |
| GK | 0.068 | 0.833 |
| DEF | 0.680 | 1.078 |
| MID | 0.961 | 1.374 |
| FWD | 0.541 | 0.793 |

Forward spread improves, but its slope remains just below 0.80. Midfielder slope
exceeds 1.20. These are important remaining failures; fitting a correction is not
the same as demonstrating calibrated predictions. Full metrics, positions,
fixture bands, candidate-size sensitivity and frozen selections are in the
[diagnostic artifact](component-calibration-diagnostics.json).

## History coverage

Cold-start ingestion previously walked the oldest/missing players without regard
to the current candidate pool. A regression replay refreshed 20 players and zero
top candidates. The revised queue allocates up to 15 requests to current candidates
and reserves five for the rest of the pool, then fills unused slots. The batch cap
stays 20. Freshness, season checks, per-player retries and worker ownership guards
remain in force. Current v4 candidate rankings take priority during rolling updates
even if an older worker publishes a newer v3 record.

An actual bounded refresh published 20 histories and recaptured GW12 before its
deadline. The local v4 primary comparison has **52/52 usable histories, zero
fallbacks**, and the wider priority set including controls is also covered. This
is a dated local input check, not a production-wide reliability guarantee or a
completed scoring result. The [live check](component-calibration-live-check.json)
records the capture time, model fingerprint, policy hash and coverage.

`check_forecast_history` is read-only by default, including when upstream snapshots
are missing. Its readiness result uses the actual primary comparison pool and the
capture's frozen policy threshold; extra control players cannot dilute missing
primary coverage. Optional warmup uses a separate durable lease and checks ownership
before publishing. Bootstrap/fixture freshness and the target deadline are re-read
before each capture. No post-deadline forecast is backfilled.

## Ownership and protecting rank

Overall selection percentage has zero weight in the deployed points heuristic,
the component models and the automatic captain optimiser. Current protect/chase
preferences adjust transfer rankings with a risk proxy, optionally against a
selected rival; they do not model field-wide effective ownership or captaincy.

The user's preference is to protect against falling behind without turning point
forecasts into popularity scores. A future recommendation layer should report
expected points and exposure relative to a relevant league or rank cohort, keep
a competitive core, and distinguish a small uncertain differential advantage from
a well-supported reason to diverge. Captaincy exposure matters separately from
ordinary ownership. Pre-deadline effective ownership needs an explicitly labelled
estimate; no rank-drop probabilities should be claimed from the current uncalibrated
variance proxy. This risk layer is a recorded requirement, not an implemented
ownership weight or an automatic instruction to captain the most popular player.
See the [official FPL glossary](https://www.premierleague.com/en/news/2683145)
for the distinction between ordinary and effective ownership.

## Prospective capture and operations

`forecast-experiment-v4` saves the heuristic, v11, v12 and fixture-only control
alongside PPG, form and available FPL estimates. It freezes both the fitted artifact
and per-model component breakdowns. Reviews use the frozen breakdown rather than
the current parameter file, and replay uses the frozen coefficients. Existing
v3 records remain separate. [Policy v2](forecast-promotion-v2.json) changes the
experiment and challenger identities while retaining every numerical threshold
from v1, including ten completed live GWs and at most 10% candidate fallbacks.
The deployment default remains `heuristic-v1`. An explicit
`FPL_PLAYER_MODEL=decision-v12-calibrated` setting now enables revised forecasts
and plans in the UI, labelled experimental. This manual opt-in does not change
the prospective promotion thresholds. See [deployment instructions](deployment.md#using-the-revised-calculations-in-the-ui).

```sh
# Read-only input coverage and scored comparisons
uv run python manage.py check_forecast_history
uv run python manage.py report_forecasts --summary
uv run python manage.py report_forecasts --experiment-version forecast-experiment-v3 --summary

# Bounded repair and an updated pre-deadline shadow capture
uv run python manage.py check_forecast_history --warm-batches 1 --capture

# Reproduce earlier-season selection and the inspected diagnostic
uv run python manage.py fit_component_challenger \
  --vaastav-root /path/to/Fantasy-Premier-League \
  --parameters-output /tmp/component-parameters.json \
  --selection-output /tmp/component-selection.json \
  --diagnostics-output /tmp/component-diagnostics.json
```

Restart the long-running FPL worker when deploying this revision so subsequent
captures use v4. Changing the source or parameter artifact changes the shadow
fingerprint and begins a separate comparable cohort. Historical calibration and
current input coverage cannot substitute for prospective accuracy and decision
results, which remain pending.

## Verification

All 70 focused forecast, calibration, history-priority, capture and review tests
pass. They include unchanged legacy forecasts, exact frozen-parameter replay,
separate v3/v4 records, preserved promotion thresholds, availability/blank handling,
bounded retries, candidate priority and read-only checks with missing sources.
Formatting and diff checks pass. A real 20-player refresh and the resulting GW12
capture verified the operational path.

The wider refresh module has 24 passing tests and eight login-redirect failures.
The same eight failures reproduce on a clean archive of committed baseline
`c93cb52` with the same environment, so this change does not claim a clean full
repository test run or alter authentication behaviour.
