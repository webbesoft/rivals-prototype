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
- Import failures retain saved data and report retry status. Empty transfer lists count as successful reads; simultaneous transfer pairs remain distinct. Partial league responses are rejected by the automatic importer, and failed player batches roll back.
- Kamal now configures a dedicated job role on the same host/shared SQLite volume. The worker waits for post-deploy migrations, processes the existing email queue too, and exposes a health-check command. This configuration has been verified locally; no production deployment or remote worker verification was performed in this epoch.

Validation: automated tests cover deadline rollover, preseason/outages, duplicate jobs and locked dispatcher restarts, expired lease recovery, retry backoff, season-safe imports, transfer batches, pagination/filter preservation, and access controls. A local synthetic dashboard request measured 82.6 ms with zero upstream requests; squad, league, status, and intel requests also assert zero upstream requests. This is a local fixture measurement, not a production latency claim. Previously, dashboard rendering could fetch the live season and synchronously sync rivals, while projections could make further FPL requests.

See [background refresh operations](background-refresh.md) for local startup, deployment checks, and recovery. Squad carousel loading and tactical cache correctness remain Epoch 3 work.

## Epoch 3 — Fast, dependable squad and tactical views

- Fix squad-history rendering and verify actual FPL picks payload fields. The current importer expects element_type in picks instead of deriving it from player metadata.
- Fetch missing history on demand, reuse picks between import steps, and avoid re-fetching every completed week on routine syncs.
- Scope history to a season; add bounded loading, empty, error, and retry states to the carousel and tactical panels.
- Key tactical caches by season/gameweek/input freshness; do not return an older “latest” result as current advice.
- Remove invented free-transfer counts: the current analysis reads a transfer-cost field as free transfers. Ask users for unavailable private information and label assumptions explicitly.
- Measure first render, cold/warm analysis, and history navigation before and after.

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
