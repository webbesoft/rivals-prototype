# How gameweek predicted scores work

Updated: 24 November 2026. Planner model: `decision-v10-planned-lineup`; shared player model: `heuristic-v1`.

For saved experiments, the component challenger, backtest commands and rollout criteria,
see [Decision engine and forecast evaluation](decision-engine.md). This file is the
human-readable specification of the current default player-points calculation.

An explicit `FPL_PLAYER_MODEL=decision-v12-calibrated` deployment setting selects
the revised experimental component calculation for the UI instead. See
[activation instructions](deployment.md#using-the-revised-calculations-in-the-ui)
and [the revised formula](component-calibration-results.md). The formula below
describes `heuristic-v1`, which remains the default when that setting is absent.

The squad planner is a hand-weighted heuristic: it starts from past FPL points,
adjusts for each upcoming fixture, and discounts for season minutes and current
availability. It then selects the legal XI and captain with the highest predicted
total. The weights are not learned from results or validated as an accurate
expected-points model.

This document describes the current default implementation. It does
not analyse this week's individual misses or establish that we outperform the
FPL Scouts. The first gameweek using the tool is not necessarily the first
gameweek of the season: the engine uses the season-to-date FPL data available.

## Which prediction are we looking at?

There are several distinct calculation paths in the app:

| Surface | Source of predictions | Selection / scope |
| --- | --- | --- |
| Squad / transfer planner and deadline digest | Our formula below | Your squad, with legal weekly XI and captain; transfer routes compare against holding |
| Public landing-page Best XI | Shared planner formula below | Whole player pool, legal XI, maximum three per club, unrestricted budget; captain doubled |
| Planner score comparison / chip advice Best XI benchmark | Planner projections | Whole eligible pool through the benchmark selector; unrestricted budget; captain doubled |
| Team analysis endpoints | Shared planner formula below | Actual upcoming gameweeks; single-week figures use that week rather than a five-week average |
| Season finishing points / rank | Team scoring pace | Separate extrapolation; not a sum of player fixture predictions |

All player-points predictions now use `PlayerForecast`. Selection constraints still
vary: your squad XI is limited to your players; public and chip benchmarks have no
budget cap. Team analysis squad aggregates can include all 15 players and are not
legal XI totals. Season finishing rank remains a separate team-pace extrapolation.
FPL `ep_next` is retained only as an evaluation baseline. The repository does not
define how FPL produces it or how the editorial Scouts choose their XI.

## 1. Player baseline: historical points

For each player:

```text
baseline = max(0, 0.60 × FPL form + 0.40 × FPL points_per_game)
```

Both fields come directly from the FPL bootstrap snapshot. We do not calculate a
custom recent-form window or adjust the past points for the difficulty of the
opponents faced. Form and season PPG overlap, so this is not two independent
pieces of evidence. A recent haul can raise both inputs.

The engine does not separately forecast goals, assists, clean sheets, saves,
bonus points or other scoring components. Position affects formation selection,
but does not change the player forecast formula. It does not use xG, xA, shots,
set-piece roles, team attack/defence strengths or bookmaker probabilities.

## 2. Fixture difficulty and venue

For each scheduled, unfinished fixture in the target gameweek:

```text
difficulty = team_h_difficulty for a home player,
             team_a_difficulty for an away player
fixture_factor = clamp(1 + (3 − difficulty) × 0.12 + venue_adjustment, 0.60, 1.40)
venue_adjustment = +0.04 at home, −0.04 away
```

Here `clamp` means restrict to the stated minimum and maximum. Missing or invalid
numeric difficulty defaults to 3 in the forecast. The visible fixture strip
instead marks missing ratings as unavailable.

| Published difficulty | Home multiplier | Away multiplier |
| --- | ---: | ---: |
| 1 | 1.28 | 1.20 |
| 2 | 1.16 | 1.08 |
| 3 | 1.04 | 0.96 |
| 4 | 0.92 | 0.84 |
| 5 | 0.80 | 0.72 |

Each difficulty step changes the multiplier by 0.12. At the same difficulty,
home versus away changes it by 0.08. These are adjustments to the baseline,
not extra predicted points or a probability of winning.

**Fixture difficulty matters, but history supplies the entire starting score.**
For example, with equal minutes and availability:

| Hypothetical player | Form | PPG | Baseline | Fixture | Prediction |
| --- | ---: | ---: | ---: | --- | ---: |
| A | 8 | 6 | 7.2 | Difficulty 5, away | 5.184 |
| B | 4 | 4 | 4.0 | Difficulty 2, home | 4.640 |

The model still prefers A despite the much harder fixture. B's fixture advantage
is about 61% (`1.16 / 0.72`), but A's baseline is 80% higher. This illustrates
your concern; it does not prove that either player is mispriced by the model.

A zero baseline remains zero even with the easiest fixture. Everyone at the same
club receives the same fixture multiplier regardless of position or role.

## 3. Minutes and availability discounts

```text
minutes_factor = clamp(season_minutes / max(90, scoring_gameweek × 70), 0, 1)
availability = clamp(chance_of_playing_next_round / 100, 0, 1)
```

`scoring_gameweek` is the latest gameweek whose deadline has passed, not a count
of completed matches or the player's appearances. Missing availability defaults
to 100%. Status must be `a` (available) or `d` (doubtful); any other status forces
availability to zero. Missing or non-numeric form, PPG and minutes default to zero.

For example, at scoring GW10, 700 season minutes reaches the minutes cap of 1;
350 minutes gives 0.5. A 75% availability flag then multiplies the score by 0.75.

These are rough discounts, not a model of expected minutes in the next match.
They cannot directly recognise a new starter, a recent return, likely rotation
or a substitution pattern. PPG/form already reflect some minutes effects, so
another minutes discount may penalise limited-minute players again.

## 4. Player gameweek prediction

```text
predicted_points_per_fixture = baseline × fixture_factor × minutes_factor × availability
predicted_points_for_GW = sum(predicted_points_per_fixture for that GW)
```

Using player B above, a 0.5 minutes factor and 75% availability produce
`4 × 1.16 × 0.5 × 0.75 = 1.74` points for that fixture.

Blanks score zero; doubles sum each fixture. Unscheduled fixtures are not assigned
to a gameweek. Finished fixtures are excluded from the forecast index. The planner
targets the next actual deadline, rather than forecasting the remainder of the
currently scoring week.

Baseline, minutes factor and current availability stay constant across the whole
forecast window. Future differences therefore come from fixtures, venue and
fixture count. There is no form decay, recovery date or predicted improvement in
playing time. An injured player forced to zero remains at zero throughout the
window, even if a return might be expected later.

## 5. Optimised XI, captain and squad score

For each week, the engine tries every supported legal formation: one goalkeeper,
3–5 defenders, 2–5 midfielders and 1–3 forwards, totalling 11. It takes the highest
projected players in each position for that formation, makes the highest projected
starter captain, and keeps the formation with the largest total:

```text
squad_GW_points = sum(predictions of 11 starters) + captain_prediction
```

Captaincy therefore counts twice. Vice-captain is the next highest projected
starter, but vice-captain fallback and automatic substitutions are not modelled.
Bench points and chips are excluded from the ordinary planner total. Selection
uses unrounded projections; displayed player schedules round to one decimal and
lineup totals to two, so displayed rows may not add up exactly.

A saved planned XI overrides optimisation for the first forecast gameweek.
Transfers inherit the outgoing player's starting/bench place and armband. Later
weeks still optimise independently. Rival forecasts use their own optimised XI.

The planner shows a Best XI score and the points gap beside your forecast for each
gameweek. Both include captaincy and exclude bench points and chips. Best XI uses
the whole eligible player pool with legal formations and a maximum of three per
club, but no budget cap. It provides a projected ceiling, not an FPL average or an
affordable transfer plan. The planner preview and chip advice share this benchmark;
cached results are keyed by gameweek, player identities, selection constraints and
unrounded projections so changed forecasts cannot reuse an old score. Draft lineup
edits hide the saved score comparison until the lineup is saved and recalculated.

“Optimised” means best under this formula and these constraints. It is not a
claim to have identified the XI that will score most in the actual matches.

## 6. Transfer recommendations are a further decision layer

The default window is five gameweeks; the planner supports 3–6, shortened at GW38.
It compares keeping the squad with shortlisted one- and two-transfer routes,
applying budget, positions and club limits. The search is bounded, not exhaustive.

```text
net_gain = route_window_points − hold_window_points − transfer_hit
```

The hit is charged once, in the first gameweek. Protect/chase preferences add a
small risk adjustment to route ranking; they do not change player forecasts or
the XI optimiser's points objective. A separate immediate choice ranks shortlisted
routes by next-gameweek net gain. Sensitivity ranges are heuristic, not calibrated
confidence intervals. Price-change warnings do not alter points predictions.

## What the early results can tell us

The code supports the concern that we may lean too heavily on past returns:
historical points set the baseline, recent form has the larger weight, and fixture
adjustments are coarse and identical across positions. Whether the fixture
coefficient should be larger needs evaluation; simply increasing it could also
make predictions worse.

We should distinguish a player forecast miss, an XI/bench decision, a captain
decision and a transfer-route decision. One gameweek cannot reliably separate
luck from persistent bias, but it can expose missing inputs or implausible outputs.

The existing decision journal freezes forecasts and lineups when a choice is
saved. Reviews use final, checked results for those frozen lineups, rather than
selecting a better XI retrospectively. They exclude chips, automatic substitutions
and later managerial changes. The existing calibration command measures error in
**net transfer gain against holding across the complete saved window**, not general
per-player or absolute squad-score accuracy. No-op holds are excluded from that
calibration. It is therefore insufficient by itself to diagnose this week's
individual forecast misses.

## Implemented evaluation and remaining decisions

The worker now saves a separate prospective experiment for each gameweek, covering
all eligible players, the default model, the component challenger, PPG, form and
available `ep_next` estimates. These records are independent of a manager saving a
transfer decision. Final results score the frozen selections and a hindsight XI
under identical selection rules.

The revised component model runs in shadow mode as `decision-v12-calibrated`,
alongside original `decision-v11` and a fixture-only control. Its fitted parameters
and history-coverage changes are documented in the
[calibration follow-up](component-calibration-results.md). Ridge fitting remains
offline as `decision-v11-ridge-standardized-v1`; these experiments do not change the
planner default. See [the evaluation guide](decision-engine.md) for assumptions,
known archive limitations and the conditions for promoting a new model. There is
still no evidence here of an advantage over the Scouts.

## Code references

- [Shared player model](../rivals/services/player_forecast.py): `PlayerForecast`.
- [Planner](../rivals/services/decision_engine.py): `load`, `prepare`, `evaluate`, `candidate`, `options`.
- [Deadline context](../rivals/services/gameweek_context_service.py): `derive`.
- [Public Best XI](../rivals/services/landing_data_service.py): `get_best_xi`.
- [Benchmark XI selector](../rivals/services/xi_benchmark.py): `best_projected_xi`.
- [Chip advice](../rivals/services/chip_advice_service.py).
- [Team analysis consumer](../rivals/services/team_analysis_service.py): `calculate_expected_points`.
- [Season pace projections](../rivals/services/projection_service.py): `project_team_finish`.
- [Frozen-plan reviews and calibration](../rivals/services/decision_review_service.py).
- [Transfer decisions and journal behaviour](transfer-decisions.md).
