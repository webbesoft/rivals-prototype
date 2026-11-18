# Transfer decisions and the hold baseline

The dashboard and My Squad open a short decision summary in a separate request. The planner at `/dashboard/decisions/` compares keeping the squad with one or two transfers at the next actual FPL deadline. Initial page rendering does not call FPL. There are no transfer-execution endpoints: saving a plan records the manager's decision journal.

## Inputs and constraints

The planner opens with recommendations, using the latest published squad and estimated bank, free transfers and selling prices. No squad confirmation or price entry is required. Optional corrections, preferences and rival selection live under “Adjust estimates or choose a rival”; individual players and selling prices are collapsed further. A short note explains that recent transfers and final prices can differ.

Free transfers start from the manager's latest correction in the same season, when available. Each observed move after that correction uses one transfer, the remainder rolls forward with one new transfer per normal deadline, and the stock is capped at five. Earlier moves are already included in a midweek correction and are not subtracted twice. Without a correction, the estimate reconstructs the allowance from recorded weekly transfer counts, ignoring the initial unlimited squad selection. Missing history does not imply five saved transfers: the fallback is one. Wildcard and Free Hit weeks preserve the saved allowance; bench boost and triple captain use normal transfer rules. A new season never inherits an old season's correction.

Bank defaults use the most recent public gameweek balance or a newer manual correction, adjusted for subsequently observed permanent transfers with known costs. Team/picks balances provide fallbacks; no known balance defaults to £0m. Selling prices use recorded acquisition costs and FPL's half-profit/full-loss rule. If acquisition history is missing, the opening season price provides an estimate. A corrected selling price also records its market price so later price changes can be reflected. Free Hit squads and bank snapshots are excluded from next-week permanent-squad defaults when chip history is available.

All estimates remain editable. Saving a rival or horizon does not turn automatic values into manual corrections or reset their timestamps. Signed, manager-bound rendered defaults distinguish edits from unchanged values, so an open form does not overwrite fresher estimates on submission. Corrections remain private to the manager and team; financial anchors carry across deadlines within their season. Public FPL data may not reveal transfers before the deadline, so the app cannot guarantee the estimate matches private live FPL values.

The engine validates 15 unique players in the 2/5/5/3 positional split, same-position replacements, at most three players per club in the resulting squad, and the final combined budget. Simultaneous funding moves are evaluated as a pair rather than requiring the expensive move to be affordable first. Regular transfer costs are four points for each move beyond the estimated free-transfer allowance. It also shows the ordinary next-week allowance, capped at five. The [Premier League transfer guide](https://www.premierleague.com/en/news/2174907) explains these transfer rules and why selling prices can differ from purchase prices.

This version compares normal transfers, without a chip. It supports two moves at this deadline, rather than an exhaustive schedule of future transfers or wildcard optimisation. Suggestions use a bounded shortlist per outgoing player: leading projections, a cheap funding option, and individually affordable alternatives. A manager can use the manual sandbox to test any available same-position alternative outside that shortlist.

## Forecast and ranking

The selected horizon is 3–6 actual gameweeks, shortened at the end of the season. Fixtures are indexed by club and event. A blank has zero fixtures; a double sums both matches. Unscheduled fixtures are not silently assigned to a gameweek.

Recommendations and route comparisons show a visible keep-versus-buy fixture strip for the next five gameweeks, shortened at GW38 and independent of the points forecast horizon. Each match shows the opponent, home/away and the published FPL difficulty for that player's club (1 easiest to 5 hardest). Doubles show both matches, blanks stay in their gameweek, and missing ratings are marked unavailable rather than assigned a default rating.

For each player and fixture, the baseline is 60% FPL form and 40% points per game. It is adjusted by home/away fixture difficulty, season minutes divided by scoring-gameweeks × 70 (capped at one), and the current availability percentage. Non-available status gives a zero projection. The model holds availability constant over the forecast window; it does not invent recovery dates. These coefficients are an explicit heuristic, not a fitted or validated probability model.

By default each scenario independently selects the highest-projected legal XI and captain for each week. The manager can instead save a planned XI, captain and vice-captain for the next gameweek. That selection is used for both holding and transfer routes: incoming players inherit the outgoing player's starting or bench place and armband. Later gameweeks still optimise the legal XI and captain. Bench points are not added, and captain points count twice. Net gain is scenario squad points minus the unchanged-squad forecast minus the transfer hit. The hold scenario is always present, with zero relative gain and no points cost. The potential future benefit of rolling is represented by retained free transfers, not fabricated future points.

The pitch appears above the transfer comparison and identifies each lineup as planned or optimised. Public FPL data cannot expose an unpublished next-deadline selection; managers can match it manually using “Edit XI and captain”. The editor validates 11 distinct squad members, one goalkeeper, a legal formation, and distinct starting captain/vice-captain. It changes private planning preferences only, scoped to the manager, team, season and deadline; financial corrections remain intact. Squad edits remap selected slots to the corrected same-position players. A lineup change invalidates cached reports and signed journal choices. Rival squads always use their own optimised forecasts, including when all 15 players match the manager's squad. Bench order, automatic substitutions and captain fallback are not modelled.

The editor uses pitch substitutions: select a starter and a highlighted bench player (or the reverse). Only legal swaps are applied, so each substitution retains 11 starters. Selecting a starter also exposes captain and vice-captain actions; substituting an armband holder passes the armband to the incoming player. Draft changes update the formation and submitted player IDs together. The old points estimate is hidden until saving recalculates it. Unsaved changes can be undone. Invalid swaps and rejected submissions show a red, focused alert with the exact player-count or formation problem; failed requests retain the draft. JavaScript is required for pitch substitutions. The server independently validates every submission.

Sensitivity uses changed player exposure and a larger spread for uncertain minutes/availability. Protecting a lead penalises additional exposure against a selected rival; chasing gives it more upside weight. Without a rival squad the comparison uses uncertainty versus holding. A recommendation still needs a positive expected gain after hits. These ranges are not confidence intervals and there is no claimed win probability.

The summary leads with predicted squad points for holding versus the recommended route, both for the next gameweek and for the exact selected range (for example GW11–15). Both totals include captaincy and deduct the transfer hit once, in the first gameweek. The window total includes the first gameweek, rather than adding another five weeks. Per-route comparisons show weekly and cumulative gains, including negative immediate returns for moves whose gains arrive later.

The full-window recommendation retains the selected protect/chase risk preference. A separate immediate-points choice ranks this deadline's shortlisted routes by next-gameweek net gain. When the choices differ, both routes and both time horizons appear in the planner and email. This can surface a short-term punt even when holding wins over the complete window; it can be saved separately to the journal. Candidate shortlists include the leading next-gameweek projections alongside the full-window and funding options. Neither search is exhaustive.

Player outlook notes distinguish scheduled fixture improvement from observed weak form and availability concerns. A fixture improvement requires later average projected player points to exceed this week by both 15% and 0.5 points; blanks and doubles are included. A form concern means reported FPL form is more than 25% below season points per game. These are explanatory flags, not extra ranking bonuses or diagnoses of lasting decline. Current form and availability remain constant in the forecast; no rebound or injury recovery date is invented. Replacement details compare outgoing and incoming player forecasts, separately from legal XI, bench and captain effects on squad points.

The main list prioritises different outgoing players and covers different positions when positive opportunities exist. Automatic routes do not repeat the same outgoing player across the list. Further replacements for one player are grouped as alternatives for that slot. The engine does not insert a losing trade just to fill a position. An explicitly requested manual route and a differing immediate-points choice can appear separately for comparison.

## Price-change warnings

The official FPL bootstrap player fields supply price progress and projections.
Squad price warnings appear beside availability warnings, on the forecast pitch,
and in deadline emails. Route timing notes also cover incoming players and feed
the evidence used for AI briefings. Price signals do not change points forecasts
or transfer rankings.

The watch threshold is our heuristic: at least 90% of a rise or fall threshold.
The next-update projection (`offset: 0`) takes precedence over current progress;
later projections are not treated as imminent changes. At 100% projected progress
the label becomes “Price rise/fall projected”. These are threshold percentages,
not probabilities or guaranteed price changes. Missing, non-finite, calibrating,
or currently locked data do not produce actionable warnings. Changed price fields
invalidate the report cache and saved-choice fingerprint.

Purchase timing notes compare a hypothetical £0.1m rise for each flagged incoming
player with the route's estimated remaining bank. Selling-price changes are not
invented: a market drop may be absorbed by FPL's profit rules, so the manager is
asked to check the actual selling value. Price timing should be weighed against
waiting for availability news.

## Mini-league scenarios

Managers can select an active subscribed league and a rival listed in that league's current-season standings. The comparison shows the points gap, shared players, unique-player forecasts, a possible rival captain, and a scenario in which the rival keeps the last published squad. Rival future transfers, free transfers, and captain choices are not known. Incomplete rival data produces an unavailable comparison rather than an invented squad or gap.

Reports are cached per manager, team, relevant FPL inputs, private planning inputs, and league membership. A membership change cannot reuse a previously accessible league comparison. FPL data retain their existing upstream cache. Report time is preparation time, not a claim that every source changed at that instant.

## Journal and honest evaluation

Saving an option requires a signed, manager-bound choice from the displayed report. The server recomputes the current report and rejects an outdated choice after relevant input/deadline changes. Repeating the same save is idempotent. Financial inputs, source/model version, forecasts, scenario lineups and the hold baseline are frozen in the record. Managers can separately mark whether they followed the plan or kept their squad. This does not place transfers in FPL.

The existing FPL worker schedules `decisions` reviews. A review only reads final, checked event scores from the matching current season. It evaluates the chosen and hold lineups saved before the deadline, including their saved captain bonuses and the original points cost. It never chooses the best XI retrospectively from actual results. Partial results remain partial; missing scores and unconfirmed hits are not replaced with zero. Archived-season records are not populated from a newer season's reused player IDs.

The review intentionally excludes chips, automatic substitutions, and later managerial changes. It measures a frozen plan's outcome against a frozen hold policy, not actual mini-league points gained because of the tool. Unknown-cost records do not get a net calibration error. No-op holds are excluded from transfer calibration so they cannot artificially improve accuracy.

Run an operational review with:

```sh
uv run python manage.py review_decisions
```

It reports the count of completed transfer-plan comparisons, mean absolute forecast error, and signed mean error. This is prospective evidence collected from saved decisions, not a historical out-of-sample validation or proof of a competitive advantage. A broader historical data set and calibration study remain future work.

## Deployment and API

Apply migrations through `0014`, collect static assets as usual, and run `fpl_worker` for ongoing journal reviews. Migration `0014` preserves the timestamps of existing financial corrections and adds per-gameweek bank snapshots; the next history refresh fills those snapshots. The live planner does not depend on a fresh background context: it verifies the season/deadline directly from the cached/public FPL event feed.

The planning API now uses the same engine. The projections route takes the internal team ID, requires the manager's own team, and applies an explicitly selected owned plan from the current season instead of ignoring it. Plans from other seasons or containing moves at other deadlines are rejected. Existing plans with no season must be recreated; their season is not guessed. Projection/suggestion responses now contain structured scenarios and assumptions rather than unconstrained player rankings.

Initial shells render without FPL requests. Interactive requests have a 25-second client timeout; each FPL HTTP operation has a four-second timeout. Client timeouts are not a global server execution deadline. A warm report avoids the bounded candidate search; source, deadline and user-input changes invalidate it.

## Published sources and season identities

The web planner now reads shared worker publications. A cold or expired source queues
refresh work and reports temporary unavailability; retry after the FPL worker runs.
It does not fetch FPL data during a page request. The current public squad is read
from saved season/gameweek rows or a published picks snapshot.

Player database IDs are season-specific. API plan transfers must reference players
from the plan's season; older plans containing unclassified legacy players must be
recreated. A plan holds at most two transfers per gameweek and 76 total; next-deadline
comparisons accept at most two moves. Plan-list responses use 100-item paginated
`count`, `next`, `previous`, and `results` envelopes.
