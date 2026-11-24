# Component challenger validation results

24 November 2026. The unchanged `decision-v11` component model reduces candidate
prediction error by **9.2%** against `heuristic-v1` on the newly evaluated 2025/26
season. Its current historical team-strength adjustment makes predictions worse,
and attacker calibration still fails the registered promotion thresholds. The
challenger remains in shadow mode; these results do not change planner forecasts.

## Evaluation scope

The [declared protocol](component-validation-protocol.json) fixes eight variants,
candidate sizes and comparisons before inspecting the 2025/26 model results. No
weights, priors or thresholds were fitted or selected. The 2024/25 development
comparison exactly reproduces the previous evaluation's default-model RMSE.
The current component source has a different fingerprint and its RMSE differs by
0.000109 (3.779527 versus 3.779418); the source CSV hash and 1,896 candidate
observations match. That season was already inspected; 2025/26 was the fresh
evaluation for this study and is now inspected too.

There are 34 usable development gameweeks and 35 evaluation gameweeks. GW1 has no
prior player pool. In 2025/26, GW31 and GW34 lack labels for 161 and 248 known players
respectively and are excluded rather than zero-filled. The artifact records the
full coverage audit and source hashes. The newer archive includes defensive
contribution fields, so the evaluation covers their scoring from 2025/26.

Each week's candidate pool is the union of the default and full challenger's top
40, selected using forecasts alone. All variants score those **same observations**:
1,992 player-gameweeks in the evaluation season. Variants cannot improve their
measured error by changing the candidate pool. Top 30 and top 50 are prespecified
sensitivity checks. All lineup comparisons use the same eligible player pool,
legal formations, three-per-club limit, unrestricted budget and doubled captain.
They do not measure transfer gains for a manager's budget or existing squad.

## Prediction accuracy

Lower RMSE and Poisson deviance are better. Mean forecasts and actuals below refer
to the common top-40 candidate pool, not the whole registered player population.

| Model or intervention | RMSE | Poisson deviance | Mean forecast | Mean actual |
| --- | ---: | ---: | ---: | ---: |
| Current points heuristic | 3.937 | 3.449 | 4.805 | 3.982 |
| Full component challenger | 3.575 | 2.922 | 4.351 | 3.982 |
| Neutral team strengths | 3.536 | 2.867 | 4.040 | 3.982 |
| Remove season and recent xG/xA | 3.585 | 2.946 | 4.297 | 3.982 |
| Season attacking rates only | 3.572 | 2.922 | 4.311 | 3.982 |
| Season minutes only | 3.580 | 2.977 | 3.984 | 3.982 |
| Withhold approximately half of histories | 3.563 | 2.924 | 4.045 | 3.982 |
| Withhold all histories | 3.589 | 3.014 | 3.691 | 3.982 |

The full challenger's mean weekly RMSE difference against the default is
**−0.341 [−0.513, −0.207]**. Its Poisson-deviance difference is
**−0.529 [−0.764, −0.333]**. Both improvements persist for candidate sizes 30 and 50.
Intervals use 5,000 paired whole-gameweek bootstrap draws, seed 1729. Negative
actual points remain in RMSE and decision outcomes; deviance excludes and counts
them. These are marginal intervals, and serial dependence is not modelled.

## What the additional inputs contribute

Neutralising team attack and defence improves mean weekly RMSE by
**−0.043 [−0.072, −0.014]** relative to the full challenger. This also improves
deviance and persists at candidate sizes 30 and 50. The development season shows
the same direction. This identifies the **current rolling-goals strength proxy**
as a development target. It does not establish that fixtures are unimportant or
that live FPL strength fields behave identically to this historical proxy. The
neutral variant retains venue baselines and all player components.

Removing xG/xA worsens mean weekly RMSE by **+0.008 [−0.018, +0.033]** and reduces
realised XI-plus-captain points by **3.63 per week [−8.46, +0.86]** relative to the
full model. These directions favour keeping underlying attacking information,
but both intervals span zero. Removal falls back to positional rate priors; it
does not substitute realised goals or prove xG superior to every alternative.

Using season attacking rates without recent xG/xA, or season minutes without
recent minutes, produces inconclusive error differences. There is no evidence
here for increasing the influence of recent observations. The complete model's
improvement over the old heuristic cannot be attributed solely to xG/xA: the
component structure, minutes, shrinkage and other scoring components also differ.
Ablation comparisons are exploratory and are not adjusted for multiple testing.

Withholding all histories slightly increases pooled error, but its paired weekly
RMSE difference spans zero. Approximately half-history loss behaves similarly.
These stress tests retain season totals and the shared availability proxy, which
already contains recent appearance information. They are conditional stress tests,
not proof that an ingestion outage or stale injury news is harmless. Grouped
history strata describe original source coverage; each variant's separate
`history_coverage` records the histories it actually retained.

## Calibration and decisions

The full model improves average optimism, but its overall candidate calibration
slope is **0.703**. Point-score slopes by position are GK **0.931**, DEF **0.925**,
MID **0.562** and FWD **0.378**. The latter two fail the registered 0.80–1.20 range.
All four positions have lower RMSE against the old heuristic, with marginal paired
intervals below zero. Better error does not by itself establish good calibration.

Fixture-band calibration also needs work. Easy, neutral and hard slopes are
0.054, 0.660 and 1.491 respectively. Easy and hard groups contain only 84 and 71
candidate observations across 17 gameweeks each; their estimates need cautious
interpretation. These are bands derived from the historical strength proxy.

Separate clean-sheet diagnostics cover all registered GK/DEF with known labels,
including non-playing players. Across 3,140 goalkeeper observations, predicted
clean-sheet count averages 0.0601 against 0.0554 actual, with slope 0.866. Across
8,929 defender observations, the averages are 0.0841 and 0.0837, with slope 0.789.
Doubles can produce counts greater than one. These count slopes are distinct
from the candidate point-score promotion checks.

The full challenger's realised XI-plus-captain difference is
**+0.69 points per gameweek [−4.49, +5.86]** against the default. XI-only difference
is −0.17 and captain difference is +0.86. Higher realised lineup returns remain
unproven despite the improvement in prediction error.

## Promotion decision

Keep `decision-v11` in shadow mode. The local database has **zero** completed live
comparison gameweeks. Its saved 22 November GW12 common candidate pool used season
fallbacks for **100%** of candidates, above the policy's 10% maximum. That is a
description of the saved capture, not a claim about current production ingestion.
Historical results cannot replace the required ten completed live gameweeks.

The next development priorities are team-strength calibration and the spread of
attacker forecasts. Fit changes on earlier development data and evaluate a new
version prospectively. Do not fit corrections to this inspected evaluation season
and describe another run on it as fresh validation.

## Reproduction and limitations

The [results JSON](component-validation-results.json) contains candidate IDs,
weekly errors and frozen selections, all variants, sensitivity checks, calibration,
paired intervals, clean-sheet counts and source hashes. Run:

```sh
uv run python manage.py validate_components \
  --vaastav-root /path/to/Fantasy-Premier-League \
  --development-seasons 2024-25 --evaluation-seasons 2025-26 \
  --output /tmp/component-validation.json
```

Use `--snapshots` instead of `--vaastav-root` for exact exported frames containing
final labels. Custom season splits or candidate sizes are marked as differing
from this study's declared protocol. The command never activates a model.

Source: [Vaastav FPL historical dataset](https://github.com/vaastav/Fantasy-Premier-League),
specifically the merged GW files for
[2024/25](https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data/2024-25/gws/merged_gw.csv)
and [2025/26](https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data/2025-26/gws/merged_gw.csv).
The adapter uses only earlier completed matches for player rates and team strengths
and excludes the potentially post-match `xP` column. It reconstructs final schedules,
uses proxy deadlines and availability, and introduces players only after a prior
observation. It is not an exact replay of live pre-deadline information. Single-next-GW
results do not validate horizon decay or transfer routes, and no calibrated score
distribution is available to assign odds to the earlier GW11 shortfall.
