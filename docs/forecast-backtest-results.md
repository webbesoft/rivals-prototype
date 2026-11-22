# Initial forecast backtest results (superseded)

This preserves the original diagnostic run. Its point-estimate conclusion was too
strong without paired uncertainty. Use [protocol v2](forecast-evaluation-results.md)
for candidate metrics, bootstrap intervals and the corrected historical inputs.

Run date: 22 November 2026. Training/development: 2022/23 and 2023/24.
Earlier evaluation split: 2024/25, 34 evaluable gameweeks and 24,254 player-gameweeks.

The experimental models reduce RMSE, but neither beats the default model on
realised XI/captain point estimates in this reconstruction. These unpaired
headline differences do not establish a winner. These results do not establish real-world superiority or a Scouts advantage.

| Model | MAE ↓ | RMSE ↓ | Mean XI points (11, no captain bonus) ↑ | Mean captain points (one copy) ↑ | Mean total (captain doubled) ↑ |
| --- | ---: | ---: | ---: | ---: | ---: |
| heuristic-v1 | 1.020 | 2.086 | 54.03 | 9.35 | 63.38 |
| components-v1-experimental | 1.022 | 1.980 | 52.50 | 9.03 | 61.53 |
| components-ridge-v1-experimental | 1.052 | 1.974 | 53.65 | 9.29 | 62.94 |
| ppg | 1.305 | 2.193 | 44.32 | 8.56 | 52.88 |
| form | 1.061 | 2.128 | 48.44 | 7.15 | 55.59 |

Every model uses the same pre-deadline-known reconstructed player pool and
unrestricted-budget legal XI, with three players per club maximum. The default
formula beats pure PPG here, while component/ridge improvements depend on the metric.
No significance test or calibrated confidence interval is claimed.

## What this run cannot establish

Historical FDR, team strength snapshots, injury flags and trustworthy `ep_next`
are unavailable in this adapter. Fixture schedules are reconstructed retrospectively,
form is approximated, and cutoffs use earliest kickoff minus 90 minutes. Weeks
with incomplete labels are omitted; blank weeks and removed players can affect
coverage. Many non-playing registered players contribute to player error metrics.
This run therefore cannot tell us whether increasing fixture difficulty weights
would improve real pre-deadline decisions.

The final 2024/25 results were read after fixing the model specification; 2023/24
was inspected during development and then included in training. Any subsequent
model tuning needs a new untouched holdout or prospective evaluation.

See [the evaluation guide](decision-engine.md) for full assumptions and the
[machine-readable results](forecast-backtest-results.json) for sample coverage,
source SHA-256 hashes, fitted coefficients and held-out weekly metrics. The raw
CSVs come from [vaastav’s historical repository](https://github.com/vaastav/Fantasy-Premier-League).
Its archived `xP` is excluded because of its documented potential lookahead.

## Reproduce

```sh
uv run python manage.py backtest_forecasts \
  --vaastav-root /path/to/Fantasy-Premier-League \
  --train-seasons 2022-23 2023-24 --holdout-seasons 2024-25 \
  --fit-ridge --output /tmp/backtest.json
```

Check the downloaded CSV hashes against the results artifact before comparing
numbers. The JSON command output also includes all model calibration metrics.
