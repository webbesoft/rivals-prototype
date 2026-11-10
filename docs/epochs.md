# RivalSpy delivery epochs

The product should answer: “What should I do before the next deadline to improve my chances of winning my mini-league?” Each epoch ends with tests, a reviewable commit, and a stop for feedback. This document is a delivery plan, not a claim that later features exist.

## Epoch 1 — Scores first (implemented)

Dashboard and squad pages put gameweek points, the matching FPL average, total points, and overall rank first. A separate small request refreshes scores automatically on opening and every two minutes. Successful snapshots are shared across pages; a failed refresh retains the saved result and labels it stale. Score refresh never runs a full team/history sync or changes the team's full-sync timestamp. Values are FPL's published, provisional scores, rather than a locally calculated live-points estimate.

Limits: caches default to process-local memory without REDIS_URL. A shared production cache should be configured for cross-worker reuse. Scores refresh while the page is open; this epoch does not provide background ingestion. Epoch 2 removes synchronous FPL requests from dashboard, squad, and league page rendering and adds a persistent ingestion worker.

## Epoch 2 — Reliable ingestion and usable intel (implemented)

- A shared, database-backed event context separates the scoring gameweek, next decision gameweek, season, and deadline. It derives rollover from actual deadline timestamps, including when is_current lags; unknown context stays unknown.
- Dashboard, squad, and league pages queue work and render stored data without calling FPL. The existing background-task queue now has recurring dispatch, per-resource leases/generations, retry backoff, and a persistent worker heartbeat. Operational details stay out of the user interface; a short notice explains when figures may be out of date.
- Profiles, results history, transfers, league standings, players, and global event context have separate successful-refresh timestamps. Transfer refreshes never advance the full-team-sync timestamp. History ingestion avoids fetching every week's picks.
- Target intervals are two minutes during matches, within six hours of a deadline, and for two hours afterward; otherwise thirty minutes. Player imports run at most every fifteen minutes in busy windows, and event context refreshes every two minutes. A gameweek/season change bypasses the normal freshness interval. These are scheduling targets, subject to queue throughput and FPL's upstream caching.
- Transfer intel and gameweek scores paginate independently, ten per page, with season/gameweek filters, stable ordering, and preserved query parameters. Open intel panels and freshness status refresh every minute. The default season follows current FPL context; “All seasons” exposes saved history.
- Import failures retain saved data and report retry status. Empty transfer lists count as successful reads; simultaneous transfer pairs remain distinct. League imports now checkpoint five pages per background job, resume after failures/restarts, and publish complete standings atomically without a total page cap. Apply migration 0011 and restart workers for this follow-up. Failed player batches roll back.
- Kamal now configures a dedicated job role on the same host/shared SQLite volume. The worker waits for post-deploy migrations, processes the existing email queue too, and exposes a health-check command. This configuration has been verified locally; no production deployment or remote worker verification was performed in this epoch.

Validation: automated tests cover deadline rollover, preseason/outages, duplicate jobs and locked dispatcher restarts, expired lease recovery, retry backoff, season-safe imports, transfer batches, pagination/filter preservation, and access controls. A local synthetic dashboard request measured 82.6 ms with zero upstream requests; squad, league, status, and intel requests also assert zero upstream requests. This is a local fixture measurement, not a production latency claim. Previously, dashboard rendering could fetch the live season and synchronously sync rivals, while projections could make further FPL requests.

See [background refresh operations](background-refresh.md) for local startup, deployment checks, and recovery. Epoch 3 below addresses squad history and tactical briefings.

## Epoch 3 — Fast, dependable squad and tactical views (implemented)

- Replaced the script-driven carousel with a season/gameweek picker and independently loaded lineup. Only the selected week is requested; the initial squad page still makes no FPL requests. Lineups use player metadata for position rather than assuming element_type exists in picks.
- Player points now come from the event live endpoint, with a separate nullable gameweek_points field. Season totals are never shown as gameweek returns. Totals respect captain, bench-boost, and zero multipliers. Missing historical scores remain unknown rather than being invented.
- Incomplete squad imports are repaired atomically; failed imports retain existing records. Full imports reuse each week's picks and skip complete records. On-demand current-season views refresh provisional scores until FPL marks the event finished and checked, then reuse final history. Archived squads never fetch another season's picks or scores. Explicit lineup reads verify the season directly when background event context is missing or stale, so existing squads can still receive their missing gameweek points.
- Lineup and own-team briefing requests have eighteen-second client timeouts, visible loading/failure states, and retry controls. Superseded requests cannot overwrite the latest loading status. Inline comparisons have a twenty-two-second client timeout. Interactive FPL requests use four-second HTTP timeouts per request; a client timeout is not a global server execution deadline.
- Tactical caches are checked before loading external data, expire after two minutes, and include season, scoring week, next decision week, successful input-refresh timestamps, squad version, and confirmed free transfers. Legacy “latest” cache entries are never used. Repeated fixture calculations and alternative rankings are reused within an analysis.
- Free transfers are no longer inferred from transfer costs, including in rival comparisons. Managers can enter an optional count on their own squad. It is stored per manager, team, season, and upcoming gameweek; zero and unknown remain distinct. The form uses the currently documented [zero-to-five free-transfer range](https://www.premierleague.com/en/news/2174907). The next gameweek requires a new confirmation; a form left open across a deadline cannot silently apply the old count to the new week.
- Briefings identify the upcoming decision week and the older squad they are based on. Unknown bank balances stay unknown, and the page prompts managers to check current balance, selling prices, point hits, and team limits. Captain options exclude players without a fixture in the decision week. Full transfer feasibility and multi-week optimisation remain Epoch 4.

Validation: all 113 tests passed at this checkpoint. Regression coverage includes real-shaped picks without position metadata, partial imports, equal gameweeks across seasons, zero/negative points and multipliers, finalising provisional scores, outages, cache invalidation, input privacy/CSRF, and unknown/rolled-over gameweeks. Local fixture measurements: initial squad render 72.7 ms with zero upstream calls; warm lineup read 1.0 ms with zero calls; cold briefing computation 2.0 ms with three mocked upstream calls, warm briefing 0.5 ms with zero calls. An 800-player fixture produced a cold briefing in 3.1 ms with three mocked upstream calls. These numbers measure local code with fixtures, not real FPL response times. Previously, a cached briefing still loaded bootstrap/fixtures first, and the carousel had no request timeout and could receive an empty gameweek list.

Apply the two new migrations when deploying. The existing worker continues routine ingestion; lineup requests fill historical squads on demand. New JavaScript is included by the existing static-assets collection step. No production deployment or live browser test was performed in this epoch; template integration tests and JavaScript loading/failure/supersession checks passed.

## Epoch 4 — A decision engine for mini-league advantage (implemented)

Dashboard and My Squad now lead with a short decision summary after the score: squad risks, a suggested transfer route or hold, captain/vice options, and the next deadline. A dedicated transfer planner compares regular one- and two-transfer routes with an unchanged squad over 3–6 gameweeks.

- Forecasts account for each scheduled fixture, blanks/doubles, season minutes, current availability, and legal weekly XI/captain selection. They are transparent heuristic estimates, not calibrated probabilities.
- The planner opens with transfer advice using the latest squad and estimated bank, free transfers and selling prices. Optional edits are collapsed; no confirmation checklist or entry of 15 selling prices is required. The engine enforces positional/club limits and the estimated combined budget, accounts for point hits, and shows the retained normal free-transfer allowance.
- Automatic comparisons cover different outgoing players and positions. Several replacements for one poor defender are grouped as alternatives for that slot rather than taking over the recommendation list. Non-positive moves are not inserted merely for variety. A manual two-transfer sandbox can test routes outside the automatic shortlist.
- Active subscribed mini-leagues and current-season rivals can be selected. Scenarios show points gaps, shared players, differentials, and possible captain exposure using the rival's last published squad. Future rival actions remain unknown.
- Protect/chase modes alter the weight of relative uncertainty against the selected rival, with positive expected net gain still required for a suggested move. No value is awarded merely for low ownership.
- A private, signed-choice decision journal freezes the forecast, inputs and hold baseline. Background reviews compare those saved lineups against final checked scores, with no retrospective lineup selection. A review command reports transfer-forecast errors and excludes holds/unknown costs from net calibration metrics.
- The existing planning API now applies owned current-season plans through the same engine, closing the previous ignored-plan behaviour. Old season-less plans require recreation rather than a guessed season.

This epoch implements immediate two-transfer scenarios, not wildcard or exhaustive future-transfer scheduling. Forecasts and retrospective fixed-lineup comparisons exclude chips and automatic substitutions. Rolling's unknown future point benefit is not invented. Historical out-of-sample validation is not claimed; prospective saved-plan reviews provide the evidence to improve calibration.

Validation: 35 new planner tests cover squad/budget constraints, hit costs, funding routes, blanks/doubles, private inputs, access controls, signed journal choices, final-score evaluation, and diverse recommendations. The complete workspace suite passed 178 tests. An 800-player, six-week fixture comparison ran in about 237 ms with mocked upstream data. A live FPL report for the linked squad took 2.65 seconds and covered all four positions with nine distinct outgoing players across five transfer options; no sell target repeated.

See [transfer decisions and evaluation](transfer-decisions.md) for the model, safeguards, limits, deployment steps and API contract. Migrations are applied locally; no production deployment or live browser test is included.

### Epoch 4 follow-up — Automatic planner defaults (implemented)

The plan is usable immediately. Bank uses the latest available public balance or newer user correction. Selling prices are inferred from acquisition costs, falling back to opening prices and the FPL profit rule. A free-transfer correction carries forward across deadlines, with observed transfers deducted, a weekly allowance added and a five-transfer cap. Wildcard/Free Hit history preserves the allowance; known Free Hit squads revert for the next plan. Missing history uses a one-transfer fallback rather than assuming five saved transfers. Corrections stay optional, season-scoped and dated; changing a preference does not reset those dates or freeze all inferred prices. Public data can lag pre-deadline moves, so one short estimate note replaces repeated confirmation demands.

Validation: all 199 tests pass, including allowance rollover, midweek corrections, chip handling, inferred sale values, optional edits and the confirmation-free planner. Django checks and migration consistency checks pass.

Secondary replacement comparisons use compact, expandable rows showing the sell target, leading replacement and net gain. Each row opens a price/hit/gain table; the section starts collapsed so it does not compete with the main recommendation. The 56 planner tests pass after this presentation update.

Apply migration `0014`; routine history ingestion fills gameweek bank/chip snapshots. The previous Epoch 3 requirement to re-enter the transfer count at every deadline is superseded for planner defaults. See [transfer decisions](transfer-decisions.md) for default sources and limits.

## Epoch 5 — Secure bring-your-own-key analysis (implemented)

Verified-email managers can connect, replace and remove a Gemini key from account settings, then request a short priority briefing from the transfer planner. Regular advice remains available without a key or if Google fails. No email digests are enabled in this epoch.

- Credentials use Fernet authenticated encryption with a dedicated external versioned keyring, separate from Django's secret. Ciphertext is bound to its manager and credential revision. A transactional rotation command and backup/recovery instructions are included.
- Authenticated, CSRF-protected writes validate access to the fixed supported Gemini model before saving. Forms never redisplay a key. Replacement/deletion cancels outstanding results; no key is exposed through admin, APIs or task arguments.
- Gemini receives a minimal planning summary and selects up to three evidence priorities. The transfer/hold recommendation must remain included. The app renders its own evidence text and figures, rejecting unknown IDs or free-form model output, so the model cannot invent a transfer, price or news claim.
- Fixed provider endpoints/model, bounded input/output, transport timeouts, no automatic retries, durable per-user request/token reservations and connection rate limits constrain use. The settings screen explains Google disclosure and potential costs. Key validation does not prove Google identity, billing status or future quota.
- The existing worker processes UUID-only tasks, checks the latest plan and credential revision, skips stale/expired work, and avoids duplicate paid requests. Result polling makes no upstream requests. Sentry excludes credential operations and captures no request bodies or local variables; provider failures are redacted.

Validation: 47 new security/provider tests pass, including execution through the existing task worker. The isolated epoch suite passes 246 tests; Django system checks and application migration checks pass.

The keyring must be configured on both web/job roles before connections are available. See [Gemini briefings operations and privacy](gemini-briefings.md) for setup, current primary provider references, exact limits, rotation, backup/restore and remaining smoke checks. Apply migration `0015`; no production deployment, real provider key or live Google charge was part of implementation.

## Epoch 6 — Deadline digests

- Lead with the planner's “Fixtures and playing-time risks” visual, immediately below a short gameweek/deadline header and before written analysis. Show the upcoming forecast XI in formation, captain/vice, all bench players, opponents with H/A, blanks/doubles, individual estimated points and visible availability/minutes flags. Label the hold/current-squad baseline clearly. Follow the next-gameweek lineup with a compact outlook across the planning window; avoid repeating a full pitch for every week.
- Use the same forecast data and selection rules as the planner. Adapt the visual for email with inline styles and an email-safe layout that reads on mobile. Names, fixtures, estimates and risk labels must remain readable when images are blocked; no scripts, gameweek switches or hover-only explanations. Include a plain-text alternative.
- Offer an explicit opt-in for verified email accounts, with a configurable lead time (initial default: four hours before the actual next FPL deadline). Use timezone-aware deadlines, not a fixed weekday schedule.
- Store a per-user, per-season, per-event delivery record. Re-evaluate changed deadlines, use bounded retries, prevent duplicate delivery, and skip missed or expired deadlines rather than send obsolete advice.
- Refresh inputs before generating the digest. After the visual, give brief transfer-versus-roll advice, captain reasoning, relevant rival scenarios, uncertainty and a link to the plan. The visual carries the squad/fixture/risk detail; do not repeat it as a wall of prose.
- Allow preview/test delivery, unsubscribe, and pausing. Separate generation and delivery states; handle ambiguous email-send failures deliberately.
- Never include provider keys in emails. Any Gemini enhancement uses the recipient's own saved key. The visual and structured advice work without Gemini; retain them if the provider fails, and state when fresh advice cannot be generated.

## Product ideas to prioritise through use

1. A “do this before the deadline” checklist, limited to the three highest-impact decisions, with expandable evidence.
2. A target-league mode showing the leader/nearest challengers, attainable points gap, and which squad differences matter next.
3. A fixture-swing watchlist and injury/minutes alerts tied to the user's actual squad.
4. A transfer sandbox that compares hold, one move, and two moves over several weeks with explicit cost and budget assumptions.
5. A post-gameweek review separating decision quality from luck, so users learn when the advice is useful.

Success measures: visits before deadlines, plans reviewed, advice usefulness feedback, recommendations acted on, freshness failure rate, page latency, and performance against a recorded baseline. Winning a league is the objective, but outcomes alone do not prove the tool caused the improvement.
