# Decision engine and forecast evaluation

Updated 24 November 2026. The app continues to use `heuristic-v1` player forecasts
inside planner `decision-v10-planned-lineup`. The default formula is documented in
[How gameweek predicted scores work](gameweek-predictions.md). The component
revision runs as `decision-v12-calibrated` in shadow mode, alongside original
`decision-v11` and a fixture-only control. The offline ridge variant remains
`decision-v11-ridge-standardized-v1`; it is not included in live shadow capture.

The [24 November component validation](component-validation-results.md) adds a
fixed-model evaluation on 35 usable GWs of 2025/26 and isolates team strengths,
xG/xA, recent rates, minutes and missing histories. The full challenger reduces
common-candidate RMSE by 9.2%, but the historical strength proxy worsens errors,
attacker calibration needs work and higher realised XI returns remain unproven.
The [fixture calibration and history follow-up](component-calibration-results.md)
fits changes on 2022/23–2023/24 and starts a separate v4 prospective experiment.
The planner default and numerical promotion thresholds are unchanged.

## One calculation, several selection problems

[PlayerForecast](../rivals/services/player_forecast.py) owns player-points forecasts.
The squad planner, deadline digest, chip advice, public Best XI and team analysis
consume it. Team analysis no longer applies its old five-fixture multiplier, and
its single-GW comparisons no longer divide a five-week forecast by five. Public
Best XI no longer substitutes FPL `ep_next` for our predictions.

Your optimised squad XI chooses from your 15 players. Public and chip Best XI
benchmarks choose from the player pool without a budget limit. They enforce legal
formations and at most three players from a club, and optimise the XI **including
its captain bonus**, rather than adding captaincy after choosing an XI. Public
player rows show individual points and identify the captain; the total doubles
captain points. All these are expected-points objectives.

Team-analysis totals across a squad can include all 15 players; sharing a player
model does not turn those aggregates into legal XI scores. End-of-season rank
projections remain a different statistic, extrapolated from team scoring pace.
FPL `ep_next`, pure PPG and pure form remain independent evaluation baselines.

## What is saved each week

The independent `forecasts` refresh job computes an experiment from the bootstrap
and fixture pair published by the context worker. Its preparation and publication
failures have their own retry/error state; they cannot roll back core snapshots.
It stores one `GameweekForecast` per season,
gameweek and experiment version (`forecast-experiment-v4`), independent of whether
any manager saves a transfer plan.

The record contains the complete raw bootstrap snapshot (including events,
availability and `ep_next`), fixtures, teams, recent histories, source capture time,
actual deadline, model-version identities, predictions and exact selected XI/captain
for each model. Shadow player-model identities are separate from the default
planner's `decision-v10-planned-lineup` identity; they cannot alter the manager's
active plan. The record retains per-model component breakdowns, the full fitted
v12 artifact, minutes-source
fallback and variance proxy. Each capture also freezes per-player usable-history
flags, full-pool/top-40/shared-candidate fallback coverage, explicit model parameters,
a model source/config fingerprint, and the registered promotion-policy hash.
IDs are scoped by season. Invalid/missing minute observations do not count as usable
history; a zero-minute observation is usable evidence of a non-appearance.

The record is refreshed with the latest successfully published pre-deadline
experiment. Publication acquires the worker ownership lock in its own durable
transaction,
checks the fresh source deadline after lock/write waits and immediately before
commit, and rolls back if that deadline has passed. It cannot be nested inside a
later core refresh commit. Commit I/O itself has no strict wall-clock upper bound.
A forecast is rejected if a deadline check is late; a missed week stays missing. We do not backfill predictions from results.
A new version must use a new identity rather than rewrite an old experiment.

The independent `forecast_reviews` job reviews these records only when the event
is both finished
and data-checked, in the matching season. It requires actual scores for the entire
forecast pool. Missing scores leave the record pending, not zero-filled. Final scoring bypasses
the provisional live-score cache. Ownership is checked under lock immediately before
an unscored-only update, so a resumed stale worker cannot overwrite its replacement.
Forecast scoring retries independently of successful manager decision reviews.
Negative and zero actual scores are retained. Completed outcomes are not repeatedly rewritten.

The landing page shows the latest five completed weeks for the default model:
predicted total, realised total, hindsight best total and XI overlap. Full player
scores, selected/hindsight IDs and all model metrics are available through the
report command. The hindsight team is a ceiling under the same pre-deadline pool,
formation and club limits, with no budget cap and captain doubled. It is not FPL's
unrestricted hindsight Dream Team, nor the editorial Scouts' selection.

Saved manager decisions and their hold-versus-transfer reviews remain separate.
They preserve their original model and frozen lineup; existing records are not
recomputed with the challenger.

## Experimental expected-minutes model

[ComponentForecast](../rivals/services/component_forecast.py) uses up to six recent
match histories from `element-summary`. Most recent weight is 1, then 0.75, then
0.75², etc. From these observations it estimates P(play), P(start), P(60+) and mean
minutes. Missing starts fall back to a 60-minute appearance indicator. Appearance
points are P(play) + P(60+).

Current FPL availability scales these quantities once. Statuses other than available
or doubtful force zero. There is no additional season-minutes multiplier on the
component score. Across later weeks, recent minutes regress toward season usage
with weight 0.85^offset. A double has a provisional 0.90 rotation factor per fixture.
These are explicit experimental assumptions, not fitted rotation probabilities.

A bounded `forecast_histories` worker refreshes up to 20 player summaries every
two minutes. It prioritises up to 15 current candidates while reserving five
places for the rest of the oldest/missing pool. Histories become refreshable after three hours and
expire after six. This avoids making hundreds of HTTP calls from a page or blocking
the context refresh on every player. The initial population takes multiple worker
cycles. Bootstrap and context seasons must match before player IDs are requested.
Per-player retry state is separate from snapshot freshness: failed requests back off
for five minutes, doubling up to one hour, and are excluded before the 20-request
limit. Every player without a usable history uses a labelled season-usage fallback.

There is no European match-calendar integration, diagnosis of a role change or
reliable injury-return source yet. Availability remains constant across the window;
the model deliberately does not fabricate recovery dates.

## Experimental points components

The challenger sums these expected scoring components per fixture:

```text
appearance + goals + assists + clean sheet + saves + bonus
− cards − goals-conceded penalties
+ defensive contribution + net penalty events − own goals
```

Goals use position-specific values: GK 10 from 2024/25 (6 earlier), DEF 6, MID 5,
FWD 4. Assists are worth 3; clean sheets GK/DEF 4, MID 1, FWD 0. See the
[official scoring explanation](https://www.premierleague.com/en/news/2174909) and
[2024 goalkeeper change](https://www.premierleague.com/ar/news/4059044).

Season xG/xA totals are converted to per-90 rates and shrunk toward position priors
with 900 prior minutes. The goal/assist priors per 90 are GK 0.005/0.01,
DEF 0.06/0.10, MID 0.20/0.18 and FWD 0.35/0.12. Missing underlying fields use the
prior. Available recent underlying stats use the same 0.75 recency weighting,
shrunk toward the season rate with 450 prior minutes. That recent influence decays
by 0.85^offset across the horizon. Rates multiply expected minutes / 90.

Team attack and defence strengths are normalised separately against league averages
for the relevant venue, with normalised strengths bounded to 0.5–2. Our own attack
divided by opponent defence scales xG/xA. Opponent attack divided by our defence
scales expected goals conceded, using provisional conceded baselines of 1.2 at home
and 1.5 away. Missing strengths are neutral. This replaces FDR in the challenger;
it does not apply FDR as another multiplier.

Team clean-sheet probability is exp(−expected team goals conceded), multiplied by
P(60+) for eligibility. This is an approximation: it does not model the timing of
substitutions and conceded goals. Saves and conceded penalties use expected integer
thresholds under Poisson distributions conditional on an appearance, then weight
the resulting threshold points by P(play). Playing probability is not applied to
the Poisson mean before nonlinear threshold scoring. A certain 90-minute appearance
retains its previous threshold expectation.

Bonus, cards, saves, penalty events and own-goal rates use recorded history with
900 minutes of shrinkage. Per-90 priors are bonus 0.2, yellow cards 0.15, red cards
0.01, GK saves 3, and zero for rare penalty/own-goal events. From 2025/26, defensive
contribution thresholds are 10 for DEF and 12 for MID/FWD; the empirical probability
has five pseudo-observations with success priors 0.15 for DEF and 0.05 for MID/FWD.
See [official defensive contribution rules](https://www.premierleague.com/en/news/4361991).
Missing event histories limit these components substantially.

The logged variance proxy covers independent goal/assist/clean-sheet terms only.
It is not a calibrated points variance, return distribution or confidence interval.
No claim is made that these hand-chosen priors are better than the existing model.

## Evaluation metrics

Headline evaluation focuses on decisions and plausible candidates:

- Top 40 forecast players per model per week (configurable to 30–50): RMSE,
  Poisson deviance and calibration by predicted-score bucket.
- The union of each model's and the reference's top candidates, from their shared
  pre-deadline coverage, scored by both models on identical observations.
- Paired weekly differences in realised XI, captain and XI-plus-captain points
  against the default. Each captain field is one copy; total adds the second copy.
- Mean differences with 95% percentile bootstrap intervals: 5,000 deterministic
  draws of whole paired gameweeks, not individual players. A zero-spanning interval
  is inconclusive, not evidence of equality. Serial dependence is not modelled.

Bucket calibration pools counts and weighted predicted/actual sums across weeks.
Top candidates are selected by predicted scores, never by actual points. Own-model
candidate pools can differ in composition; use the shared-union results for direct
error comparisons. Each challenger has a different shared union with the reference.

Poisson deviance cannot accept negative net FPL points. RMSE and decision outcomes
retain them; deviance excludes and counts them explicitly. Predicted Poisson means
are floored to 1e-6 only for deviance, with a count of affected predictions. This is
a scoring diagnostic, not a claim that total FPL points have a Poisson distribution.

Both commands now also report paired **weekly error** differences and GW-bootstrap
intervals on the common candidate pool, including by position, fixture band and
history/fallback stratum. Player observations provide more information about errors
than one XI/captain outcome per GW, but players in the same GW are correlated;
bootstrap resampling still draws whole GWs rather than pretending players are
independent. Pooled errors and mean weekly error differences are both shown.

Every model's point errors, ranks, calibration, candidate errors and frozen XI/captain
contributions are stratified by usable-history versus season fallback. XI and captain
strata are contributions of the same saved selection; there is no reoptimisation
inside a stratum. Contributions add back to the total. Empty strata have no error
estimate rather than a fabricated zero error. Unknown/constant calibration slopes
are null and cannot satisfy a promotion criterion.

Clean-sheet count calibration is reported separately for GK and DEF, by fixture
band and history stratum, using frozen predicted clean-sheet points / 4 and final
FPL clean-sheet counts. Labels require known minutes and clean-sheet fields;
missing labels are excluded and counted. Doubles can have expected/actual counts
greater than one, so this is count calibration, not a single-match Bernoulli claim.
These diagnostics cover all GK/DEF in the frozen pool, including non-playing
registered players. Candidate-pool point calibration remains the promotion check.

Whole-pool MAE/RMSE/bias, tied-rank Spearman within position, fixture/position/bucket
calibration and hindsight regret remain diagnostics. Whole-pool MAE can favour low
predictions across many non-playing registered players. It is not the promotion
criterion, and lower RMSE with higher MAE is not contradictory.

PPG/form remain direct baselines. Exact snapshots can evaluate `ep_next` when finite;
CSV reconstructions explicitly mark it **untested**, never substituting archived
`xP`. Pairwise XI comparisons restrict to shared coverage when a baseline is missing
player estimates, rather than comparing lineups from unequal player pools.

## Running it

For controlled component ablations and missing-history stress tests, use
`validate_components`; its [protocol and results](component-validation-results.md)
include the command and same-candidate comparison rules. `backtest_forecasts`
continues to handle the broader baseline and optional ridge evaluation below.

Apply migration `0024`, then run the existing `fpl_worker`. No extra daemon is
required. Normal context/decision jobs capture and score future experiments.

```sh
uv run python manage.py migrate
uv run python manage.py fpl_worker
uv run python manage.py report_forecasts --season 2026/27
uv run python manage.py report_forecasts --season 2026/27 --summary
uv run python manage.py report_forecasts --season 2026/27 --export /tmp/forecasts.json
```

The report/export defaults to experiment v4. Use `--experiment-version forecast-experiment-v3`
or `forecast-experiment-v1` to inspect old records separately; versions are not
merged into duplicate GW frames. `check_forecast_history` reports current usable
candidate history coverage; see the [follow-up operations](component-calibration-results.md)
for bounded warmup and capture commands.
`--summary` scores the frozen forecasts, reports candidate calibration and paired
bootstrap intervals, and shows progress toward at least ten completed **live** GWs.
Its primary summary includes only records with the current live provenance, model
fingerprint and policy hash; excluded completed records are listed explicitly.
Historical backtests cannot count toward that live minimum. No automatic promotion
is triggered by reaching it. Ten GWs are not ten calendar weeks: breaks, postponed
fixtures and incomplete results can extend collection, so no fixed promotion date
is promised.

The export contains completed records only, as JSON frames with pre-deadline inputs
and a separate `actual` map. Imported snapshots require timezone-aware capture and
deadline timestamps, capture before deadline, histories before capture, and a
scoring GW earlier than the target. Models never receive the `actual` map as a
feature. These checks cannot prove the provenance of an arbitrary user-supplied
file: its timestamps and source content still need to be trustworthy.

For local historical reconstruction, use a checkout of
[vaastav/Fantasy-Premier-League](https://github.com/vaastav/Fantasy-Premier-League):

```sh
uv run python manage.py backtest_forecasts \
  --vaastav-root /path/to/Fantasy-Premier-League \
  --train-seasons 2022-23 --holdout-seasons 2023-24 \
  --fit-ridge --top-k 40 --output /tmp/backtest.json
```

For exact snapshots, replace `--vaastav-root` with `--snapshots /path/to/frames.json`
(or a `.jsonl` file). Season identifiers must match the source file (`2026/27` for
live exports, `2023-24` for the historical directory convention). JSON output keeps
weekly results, sample counts, split summaries and the fitted ridge artifact.

The optional ridge model fits component coefficients per position on explicitly
specified earlier training seasons only. It fits corrections to the component sum,
shrunk toward that sum with L2 penalty 100 applied to **training-standardised**
component features, separately by position. Means and population standard deviations
are fitted on training observations only and frozen in the artifact; constant
columns use scale 1. The intercept is unpenalised. The objective is summed squared
residual error plus 100 times squared standardised correction coefficients.
The artifact records feature order, means, scales, standardised corrections, raw
coefficients, penalty, training provenance/seasons and sample counts. Artifacts
without standardisation are rejected by the predictor and must be refitted.
The new version has been refitted on rolling-strength data; it remains offline.
Rolling-goal proxies and live FPL strengths can still differ in distribution, so
refitting does not by itself justify applying ridge live. Training and holdout cannot overlap or be chronologically reversed.
Training metrics are in-sample; judge it on the holdout. The fitter does not search
hyperparameters on that holdout. LightGBM is not implemented.

## Historical limitations and promotion criteria

See [protocol v3 results](forecast-evaluation-v3-results.md) for standardised ridge,
fallback/clean-sheet diagnostics and candidate metrics,
paired bootstrap intervals, coverage audit and source hashes. The earlier run is
preserved as a superseded diagnostic. The 2024/25 evaluation season was already
inspected; rerunning a revised protocol does not make it an untouched holdout.

The CSV adapter reconstructs PPG, five-GW points form, cumulative minutes and
underlying totals strictly from earlier GW rows whose kickoff plus a conservative
three-hour completion allowance precedes the target cutoff. Current-GW scoring
columns never feed the forecast. Doubles sum labels; players enter the pool only
after a previous observed row. It does not read final-season `players_raw` totals.

This is **not an exact replay of our deployed heuristic**. It uses an earliest
kickoff minus 90-minute deadline proxy, reconstructed final fixture schedules,
an approximation of FPL form and common recent-match availability proxies rather
than historical injury news. Both models receive the same prior-match information:
recency-weighted played status and minutes over up to six matches. The default uses
the derived P(play) as its availability input. The component model derives P(play)
from those minutes already, so applies the proxy as a cap rather than multiplying
that evidence again. Real pre-deadline FPL availability flags retain their ordinary
multiplicative handling in live shadow mode.

Historical team strengths now use the last ten completed team matches, deduplicated
by team and fixture, with five pseudo-matches at venue priors (home GF/GA 1.5/1.2;
away 1.2/1.5). Attack is goals-for rate / venue prior; defence is venue prior /
goals-against rate. The component model uses separate strengths directly. The old
heuristic receives a coarse proxy from the same information: opponent venue
strength = sqrt(attack × defence), then round(3 + 2 × (strength − 1)), bounded to
1–5. No historical FDR snapshot or future match score is used. These are disclosed
proxy features, not fitted coefficients. Player identity/club comes from the last
eligible earlier row.
Weeks with incomplete labels for that known pool are skipped, not zero-filled;
missing/blank fixtures in the archive can therefore reduce the sample. Every GW
is audited with evaluated/skipped status, reason, phase (GW1–5 versus established),
known-player count and missing-label player IDs. In 2024/25, GW1 lacks history;
GW15, GW29 and GW34 lack complete labels. These are not mainly early-season
exclusions, and can bias coverage around disrupted schedules.

The dataset explicitly warns that `xP` is scraped from `ep_this` after a gameweek
and may contain lookahead; we exclude it entirely. Historical `ep_next` is therefore
unavailable in this adapter. Weekly updates also stopped after 2024/25. See the
[dataset's source notes](https://github.com/vaastav/Fantasy-Premier-League#xp-column--potential-lookahead-for-ml-models).

## Registered promotion policy

[Policy v1](forecast-promotion-policy.json) was registered on 22 November 2026 for
v3. [Policy v2](forecast-promotion-v2.json), registered on 24 November, applies the
same numerical thresholds to the separate v4/v12 cohort. The policy hash is frozen
in each capture. These thresholds
are prospective, not selected from the historical results. All checks are required
for a manual promotion review; the app never activates a challenger automatically.

| Check | Fixed threshold |
| --- | --- |
| Live evidence | At least 10 completed, data-checked live GWs from one model fingerprint and policy hash |
| Primary candidate pool | Union of both models' top **40**, restricted to shared pre-deadline coverage |
| Primary errors | Lower pooled RMSE and Poisson deviance; each mean weekly challenger-minus-old difference < 0 and its 95% GW-bootstrap interval entirely below 0 |
| Position safeguard | For each of GK/DEF/MID/FWD, RMSE-difference interval upper endpoint ≤ 0; insufficient evidence is not a pass |
| Calibration | Actual-points-on-predicted-points slope **0.80–1.20 inclusive**, separately for each position and easy/neutral/hard fixture band in the common candidate pool |
| Group support | At least **50 player-GW observations and 10 GWs** for each required position and fixture group; constant predictions/undefined slopes fail support |
| Fallback exposure | At most **10%** of common candidate observations use the season fallback; report both strata regardless |
| XI/captain sanity | Mean paired XI points and captain points must each be **positive**; report their intervals, but those intervals need not exclude zero |
| Sensitivity | Report prespecified N=30 and N=50 checks; neither can replace N=40 because it looks more favourable |

All intervals use 5,000 paired GW draws with seed 1729. Position intervals are
marginal, not simultaneous family-wise guarantees. Poisson deviance retains the
explicit negative-label exclusion and positive-mean floor described above.
Blank/unknown fixture bands remain reported and stay in the primary errors; they
do not substitute for the three required rated fixture groups. Clean-sheet GK/DEF
calibration must be inspected separately and any remaining bias recorded in the
promotion decision.

The primary gate is player-level candidate error with GW-level uncertainty.
XI/captain point estimates are sanity checks, not the deciding significance test.
Ten GWs is a minimum, not a deadline: a missing group or wide interval means keep
collecting. Historical results can guide development but cannot satisfy this policy.
Changing coefficients, priors, source/config code or thresholds starts a new
fingerprint/cohort; do not pool the changed experiment with the old one to reach ten.

## Tuning the guessed constants

The current 0.75 recency weight, 0.85 horizon decay, 900/450 prior minutes and 0.90
double rotation remain **untuned hypotheses** in `forecast_parameters.py`. Their
values are frozen with each live forecast. This update fixes ridge scaling and
measures their outcomes; it does not claim to have fitted those constants.

Use inner chronological validation on the training seasons only, with expanding
prefixes and later validation blocks: GW1–10 → GW11–15, GW1–15 → GW16–20,
GW1–20 → GW21–25, GW1–25 → GW26–30, GW1–30 → GW31–38. Build each validation
frame from information available before its own deadline; fitted ridge/scalers,
if used, are refitted on the preceding prefix only.

Tune one parameter at a time, in this order: minutes/recency weight first, then
season shrinkage, recent shrinkage and double rotation. Predeclare grids and choose
by mean validation common-candidate RMSE, with Poisson deviance as a tie-break;
prefer the current setting on an exact tie. Keep held-out/live labels out of tuning.
For each selected setting, preserve fold counts, coverage, chosen grid and scores
in an artifact and assign a new model/config identity before shadow collection.
The horizon decay cannot be identified from next-GW-only data (offset is always
zero); tune it only on independently frozen multi-horizon forecasts. Do not claim
a fitted decay from this harness. European-rotation adjustments need their own
schedule features and evaluation rather than guessing another discount.

A future promotion should activate the tested shadow model and give the planner
and caches a new identity, preserving the default/shadow distinction in historical
records. Current work intentionally stops short of that promotion.
