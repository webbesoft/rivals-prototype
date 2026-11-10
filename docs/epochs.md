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

## Epoch 4 — A decision engine for mini-league advantage

Build evidence-based recommendations before adding AI prose. The default home view should be a short ordered action list: urgent squad risks, best transfer or roll, captain/vice choice, and approaching deadline.

- Plan across the next 3–6 gameweeks, using fixtures (including blanks/doubles), minutes risk, availability, form, price, team limits, and positional constraints.
- Compare transfers against holding and rolling. Show expected gain after point hits, opportunity cost, affordability assumptions, and uncertainty. Selling prices and available free transfers may need user input; public data cannot establish all private team state.
- Rank realistic alternatives for each weak squad slot, and allow a two-transfer route or wildcard scenario. Separate model estimates from observed data.
- Choose a target league and key rivals. Show points gaps, shared players, captain exposure, and the gains/losses driven by differentials. Pre-deadline rival transfers and squads are unknown; show scenarios rather than claim knowledge.
- Support protecting a lead versus chasing a gap. Show conservative and higher-variance options with reasons, without treating low ownership alone as evidence of quality.
- Keep a decision journal: recommendation, data timestamp, assumptions, user's choice, and later outcome. Backtest against a hold baseline and evaluate projection calibration rather than cherry-picked wins.

## Epoch 5 — Secure bring-your-own-key analysis

Start with one supported provider (Gemini is a candidate), after checking its current API and data-handling documentation. AI explains the structured decision engine's evidence; it should not invent fixtures, prices, rules, or player news.

- Save keys encrypted using authenticated encryption and a dedicated, externally managed encryption key; plan versioned rotation and backup/restore. Do not reuse Django's session secret as the encryption key.
- Accept keys through authenticated, CSRF-protected writes, show only a masked indicator, and support replacement and deletion. Never return plaintext through forms, APIs, admin, logs, tracing, error reporting, task arguments, or emails.
- Restrict requests to fixed provider endpoints and approved models; do not accept arbitrary provider URLs. Apply timeouts, per-user quotas, token/spend bounds, and rate limits.
- Isolate users' credentials and analysis data; test cross-user access and failure redaction. Minimise data sent to the provider and explain the disclosure and costs to users.
- Queue credential references rather than raw keys, decrypt only at execution, and invalidate outstanding work when a key is deleted. Treat imported news as untrusted content.
- Require provider account verification before enabling AI runs. Keep deterministic advice available if a key is missing, invalid, or rate-limited.

## Epoch 6 — Deadline digests

- Offer an explicit opt-in for verified email accounts, with a configurable lead time (initial default: four hours before the actual next FPL deadline). Use timezone-aware deadlines, not a fixed weekday schedule.
- Store a per-user, per-season, per-event delivery record. Re-evaluate changed deadlines, use bounded retries, prevent duplicate delivery, and skip missed or expired deadlines rather than send obsolete advice.
- Refresh inputs before generating the digest. Include freshness, risks, best transfer versus roll, captain options, relevant rival scenarios, uncertainty, and a link to the plan.
- Allow preview/test delivery, unsubscribe, and pausing. Separate generation and delivery states; handle ambiguous email-send failures deliberately.
- Never include provider keys in emails. Use the structured digest if the provider fails, and state when fresh advice cannot be generated.

## Product ideas to prioritise through use

1. A “do this before the deadline” checklist, limited to the three highest-impact decisions, with expandable evidence.
2. A target-league mode showing the leader/nearest challengers, attainable points gap, and which squad differences matter next.
3. A fixture-swing watchlist and injury/minutes alerts tied to the user's actual squad.
4. A transfer sandbox that compares hold, one move, and two moves over several weeks with explicit cost and budget assumptions.
5. A post-gameweek review separating decision quality from luck, so users learn when the advice is useful.

Success measures: visits before deadlines, plans reviewed, advice usefulness feedback, recommendations acted on, freshness failure rate, page latency, and performance against a recorded baseline. Winning a league is the objective, but outcomes alone do not prove the tool caused the improvement.
