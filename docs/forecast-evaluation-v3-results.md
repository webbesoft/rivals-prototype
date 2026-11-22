# Rolling-strength evaluation with standardised ridge

Protocol v3, 22 November 2026. Training: 2022/23 and 2023/24; evaluation:
34 usable GWs of 2024/25. This is a previously inspected evaluation season,
not a new untouched holdout. Historical results guide development only.

Rolling team strengths and the old heuristic’s derived difficulty bands are active
in this run. The original neutral-fixture experiment cannot provide fixture-handling
evidence. This run evaluates the complete model; it does not isolate fixture effects
from minutes/per-90 effects without an ablation.

Ridge was refitted on the rolling-strength features after per-position training-only
standardisation. Penalty 100 acts on standardised residual corrections; its intercept
is unpenalised. The artifact includes frozen means/scales and both coefficient forms.
Ridge remains offline and is absent from the live `decision-v11` shadow forecast.

## Primary candidate-error diagnostics

Same observations for both models: union of their forecast top 40. Differences are
challenger minus reference. Intervals bootstrap whole paired GWs (5,000 draws,
seed 1729). The interval is for the mean weekly error difference; pooled RMSE is
also reported and weights player observations.

| Model | Pooled RMSE, model / old | Mean weekly RMSE difference [95% interval] | Mean weekly Poisson-deviance difference [95% interval] |
| --- | ---: | ---: | ---: |
| decision-v11 | 3.779 / 3.975 | -0.191 [-0.322, -0.079] | -0.341 [-0.534, -0.169] |
| decision-v11-ridge-standardized-v1 | 3.741 / 3.974 | -0.224 [-0.352, -0.118] | -0.389 [-0.575, -0.228] |

Negative realised points remain in RMSE and decision outcomes. They are excluded
and counted for Poisson deviance; predicted means are floored to 1e-6 for deviance
only. This is a diagnostic for net FPL scores, not a Poisson generative-model claim.

## XI/captain sanity checks

| Model | XI mean difference [95% interval] | Captain mean difference [95% interval] | Total mean difference [95% interval] |
| --- | ---: | ---: | ---: |
| decision-v11 | -1.941 [-6.647, +2.647] | -0.235 [-1.412, +0.971] | -2.176 [-6.941, +2.559] |
| decision-v11-ridge-standardized-v1 | +0.147 [-4.118, +4.500] | -0.353 [-1.706, +0.971] | -0.206 [-4.235, +3.912] |

Decision-outcome intervals still span zero. This is inconclusive, not proof of
equivalence. Candidate errors are the primary future gate; these return point
estimates and intervals are sanity checks. Multiple marginal intervals are not
simultaneous family-wise guarantees, and the bootstrap does not model serial dependence.

## History/fallback and clean-sheet checks

The JSON stratifies errors, rank/calibration diagnostics, candidate comparisons and
frozen XI/captain contributions by usable history versus fallback. It logs source
coverage and candidate fallback shares. The historical adapter admits a player only
after a prior observation, so fallback share in this run is **0%**. There is no
historical evidence about fallback performance here; an empty fallback stratum is
not a zero-error result. Live ingestion will measure that missing operating regime.

Clean-sheet diagnostics use all registered GK/DEF, including non-playing players.
Predicted counts are the frozen clean-sheet component / 4; actual counts come from
labelled FPL clean sheets with 60-minute eligibility. Doubles may have counts > 1.

| Position | Labelled player-GWs | Mean predicted count | Mean actual count | Calibration slope |
| --- | ---: | ---: | ---: | ---: |
| GK | 2551 | 0.0718 | 0.0635 | 0.708 |
| DEF | 8121 | 0.0872 | 0.0794 | 0.716 |

The means are high and slopes are below one in this reconstruction, indicating
further clean-sheet calibration work. These all-pool count slopes are distinct from
the common-candidate point-score slopes required by the promotion policy.

## Interpretation and reproducibility

Historical `ep_next` remains **untested**. The 2024/25 exclusions remain GW1
(no prior history), GW15/GW29/GW34 (missing labels). Earlier seasons and affected
player IDs are audited in the JSON. Reconstructed final schedules, proxy deadlines
and historical availability estimates remain limitations.

The [registered promotion policy](forecast-promotion-policy.json) requires at least
ten completed live GWs, primary error improvements with intervals below zero,
position safeguards, numeric calibration thresholds, adequate group support and
low fallback exposure. This historical run cannot satisfy those live criteria.

See [machine-readable v3 results](forecast-evaluation-v3-results.json) for source
hashes, the scaler/coefficient artifact, position/fixture/history calibration,
uncertainty and coverage; [the engine guide](decision-engine.md) documents the
frozen policy and the training-only tuning procedure. The guessed constants have
not been tuned or promoted using these evaluation results.

```sh
uv run python manage.py backtest_forecasts \
  --vaastav-root /path/to/Fantasy-Premier-League \
  --train-seasons 2022-23 2023-24 --holdout-seasons 2024-25 \
  --fit-ridge --top-k 40 --output /tmp/backtest-v3.json
```
