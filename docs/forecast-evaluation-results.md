# Forecast evaluation: candidates and paired decisions (protocol v2)

Superseded by [protocol v3](forecast-evaluation-v3-results.md), which standardises
ridge features, stratifies history/fallback metrics and adds clean-sheet checks.
This earlier rolling-strength run remains preserved; its ridge coefficients are
not suitable for live use.

Protocol v2, 22 November 2026. Training: 2022/23 and 2023/24. Evaluation:
2024/25, 34 usable gameweeks. This season was inspected in the earlier diagnostic
run; it is not a new untouched confirmatory holdout. Prospective shadow records
are the next clean evaluation source.

The component and ridge challengers improve candidate-pool errors. Their realised
XI/captain differences remain statistically inconclusive under the weekly bootstrap.
Whole-pool MAE is diagnostic, not the promotion criterion. Lower RMSE and higher
MAE can coexist in this skewed points distribution.

## Candidates the models would consider

Top 40 per model per week are selected by forecasts, never realised scores.
Own-pool figures describe each model’s selection; use the common union of its top
40 and the reference model’s top 40 for an identical-cohort comparison. The union
is constructed after restricting to the pair’s shared pre-deadline coverage.

| Model | Own top-40 RMSE | Own top-40 Poisson deviance | Shared union RMSE, model / old | Shared union deviance, model / old |
| --- | ---: | ---: | ---: | ---: |
| heuristic-v1 | 4.066 | 3.423 | — | — |
| decision-v11 | 3.968 | 3.228 | 3.779 / 3.975 | 3.086 / 3.431 |
| decision-v11-ridge | 3.922 | 3.127 | 3.736 / 3.984 | 3.001 / 3.434 |
| ppg | 4.460 | 4.751 | 4.220 / 3.821 | 4.245 / 4.012 |
| form | 4.354 | 3.987 | 4.333 / 3.918 | 3.983 / 3.352 |

Each challenger has its own reference-union cohort, so reference values differ
between rows. Both models use the same source information: rolling team goals
and a recent-match availability proxy. Their minutes/points models still differ.

Poisson deviance is a scoring diagnostic, not a claim that net FPL points follow
a Poisson distribution. Negative realised points remain in RMSE and XI/captain
outcomes but are excluded from deviance. Non-positive predicted means are floored
at 1e-6 for deviance only. Counts are explicit in the JSON. Calibration by predicted
one-point bucket is saved for both own and common-union candidate pools, weighted
by player-observation counts rather than averaging bucket means equally.

## Paired weekly outcomes versus the old model

Values below are mean weekly differences, with 95% percentile bootstrap intervals.
Positive means the challenger scores more points. Resampling draws whole gameweeks
with replacement, preserving the paired comparison (5,000 draws, seed 1729).

| Challenger | XI difference (11 players) | Captain difference (one copy) | Total difference (captain doubled) |
| --- | ---: | ---: | ---: |
| decision-v11 | -1.94 [-6.65, +2.65] | -0.24 [-1.41, +0.97] | -2.18 [-6.94, +2.56] |
| decision-v11-ridge | -1.18 [-5.41, +3.21] | +0.18 [-1.03, +1.38] | -1.00 [-5.29, +3.50] |

Intervals span zero for both challengers. We cannot distinguish a gain from a
loss under this evaluation; that is not proof of equivalence. The bootstrap assumes
exchangeable gameweeks and does not model serial dependence. No hyperparameter
search or promotion decision is based on these intervals.

## Coverage and remaining limitations

| Season | Evaluated | Skipped (GW: reason) |
| --- | ---: | --- |
| 2022-23 | 31 | 1: no_prior_player_pool; 7: absent_from_archive; 8: missing_labels; 12: missing_labels; 25: missing_labels; 28: missing_labels; 32: missing_labels |
| 2023-24 | 31 | 1: no_prior_player_pool; 2: missing_labels; 17: missing_labels; 18: missing_labels; 26: missing_labels; 29: missing_labels; 34: missing_labels |
| 2024-25 | 34 | 1: no_prior_player_pool; 15: missing_labels; 29: missing_labels; 34: missing_labels |

Missing-label exclusions are largely established-season weeks, not just the opening
weeks. They can bias coverage around disrupted/blank schedules. Missing labels
are not assumed zero. The JSON includes affected player IDs and early/established
phase flags. GW1 is unavailable because the adapter does not invent prior histories.

Fixture strength uses the last ten completed team matches, deduplicated by
club/fixture, with five venue-prior pseudo-matches. Only earlier GW rows that finish
before the cutoff contribute. Attack uses goals for; defence uses inverse goals
against. Both models receive these estimates: the heuristic gets a derived coarse
FDR proxy and the component model gets separate attack/defence strengths.

Both models use the same recency-weighted six-match played/minutes information
as the historical availability proxy. The component model caps its already derived
P(play)/minutes with that proxy rather than multiplying the same evidence twice.
This still approximates real availability; it cannot recover injury news.

Final fixture schedules and a kickoff-minus-90-minutes deadline proxy remain
limitations. Historical `ep_next` is explicitly **untested**; archived `xP` is excluded.
Use [the engine guide](decision-engine.md) for formulas and shadow operations,
and [the results JSON](forecast-evaluation-results.json) for source hashes,
coefficients, bucket calibration, paired weekly differences and coverage.

## Reproduction provenance

The exact v2 implementation revision is **not present in the available project
history**. The first reachable checkpoint containing this report,
`d477272568f0aad0fbf77cd37435ea0f933377bb`, already contains the v3 standardised
ridge implementation; it is not the revision that produced these v2 results.
`git log --all -- docs/forecast-evaluation-results.md rivals/services/forecast_backtest.py`
contains no earlier v2 source revision. We cannot identify an exact revision without
recovering that original source snapshot, and do not substitute the current code.

The invocation below is retained only as the recorded historical invocation. It
must be run from the original v2 source snapshot **if recovered**, not the current
v3 implementation. Until then, the preserved v2 results cannot be reproduced
exactly from the available Git history. For the current protocol, use the
[v3 report](forecast-evaluation-v3-results.md).

```sh
uv run python manage.py backtest_forecasts \
  --vaastav-root /path/to/Fantasy-Premier-League \
  --train-seasons 2022-23 2023-24 --holdout-seasons 2024-25 \
  --fit-ridge --top-k 40 --output /tmp/backtest-v2.json
```

`--top-k` can be set to 30 or 50 for a prespecified sensitivity analysis. Do not
choose the most favourable cutoff after inspecting results and call it confirmatory.
