# RivalSpy delivery epochs

The product should answer: “What should I do before the next deadline to improve my chances of winning my mini-league?” Each epoch ends with tests, a reviewable commit, and a stop for feedback. This document is a delivery plan, not a claim that later features exist.

## Epoch 1 — Scores first (implemented)

Dashboard and squad pages put gameweek points, the matching FPL average, total points, and overall rank first. A separate small request refreshes scores automatically on opening and every two minutes. Successful snapshots are shared across pages; a failed refresh retains the saved result and labels it stale. Score refresh never runs a full team/history sync or changes the team's full-sync timestamp. Values are FPL's published, provisional scores, rather than a locally calculated live-points estimate.

Limits: caches default to process-local memory without REDIS_URL. A shared production cache should be configured for cross-worker reuse. Scores refresh while the page is open; this epoch does not provide background ingestion. Existing synchronous dashboard projections/rival updates can still delay the initial page. Epoch 2 addresses those requests and the remaining manual-sync burden.

## Epoch 2 — Reliable ingestion and usable intel

- Separate “current scoring gameweek”, “next decision gameweek”, season, and next deadline in a common context. Never silently substitute GW1 during upstream failures.
- Move network-heavy rival/league/player updates out of page requests into the existing task infrastructure; document and verify the deployed worker. Freshness policies should vary for active matches, deadline windows, and quiet periods.
- Track successful refresh times per data type, with deduplication, retries, failure visibility, and last-known-good data. A transfer-only sync must not mark the whole team as synced.
- Paginate transfer intel and gameweek results independently, preserving filters and page parameters. Include gameweek/season filters and deterministic ordering.
- Test deadline rollover, outages, duplicate jobs, and multi-season histories. Measure initial dashboard latency and upstream request counts.

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
