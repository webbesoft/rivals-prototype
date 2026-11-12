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

The keyring must be configured on both web/job roles before connections are available. See [AI briefings operations and privacy](gemini-briefings.md) for setup, current primary provider references, exact limits, rotation, backup/restore and remaining smoke checks. Apply migration `0015`; no production deployment, real provider key or live Google charge was part of implementation.

### AI briefing follow-up — Usage audit and spending controls (implemented)

New generation attempts have a durable provider-call claim before submission and append-only lifecycle events. Origins, reservations, reuse/denials, provider outcomes and allowlisted token metadata remain traceable even when output validation fails or keys are removed. Unknown usage is retained as unknown; it never becomes zero or a refunded allowance. Atomic reservations enforce the existing caps; reported usage overages pause further generation until reviewed. `AI_GENERATION_ENABLED=False` stops new/queued paid requests across web/job roles.

Validation: 21 audit tests and all 99 briefing/delivery tests pass. Django system and application migration checks pass; migration `0018` is applied locally.

Apply migration `0018`. `manage.py ai_usage_report --user-id ID --days 7 --events` supports investigations without exposing keys, prompts, provider bodies or email addresses. Reservations and provider reports are not an invoice. See [usage audit operations](gemini-briefings.md#usage-audit-and-billing-investigations).

## Epoch 6 — AI briefings by deadline email (implemented)

AI briefings are the deadline emails. One account settings page manages the Gemini connection and email delivery; planner actions preview the same briefing. Key connection alone never opts a manager into email.

- The planner and email lead with the same forecast XI/bench and fixtures/playing-time risks, below a short gameweek/deadline header. Email uses inline-styled text/tables, visible captain/vice and risk labels, a compact multi-week outlook and a plain-text alternative. No external images or scripts are needed.
- Verified managers explicitly opt in, choose 1–24 hours before the actual FPL deadline (four by default), pause or unsubscribe. Previews and test emails do not subscribe the account or replace scheduled delivery.
- Fresh public player/fixture, financial/history/transfer and published lineup inputs feed the existing decision engine before generation. Gemini prioritises the engine's evidence using the recipient's saved key and the existing shared usage limits. Missing/revoked keys, quota exhaustion and provider failures retain the fresh visual and structured advice with an explicit fallback notice.
- One scheduled record per manager/season/event tracks generation separately from delivery. Changed deadlines invalidate pending work; versioned claims and leases recover abandoned generation. Three bounded attempts and a thirty-minute delivery window prevent endless retries or obsolete emails. Submitted or ambiguous email sends are never automatically repeated.
- Settings, signed unsubscribe confirmation, ownership/CSRF controls, current-email/consent rechecks, worker tasks and recovery behavior are covered by automated tests. No keys or raw provider failures reach email or queue arguments.

Validation: 31 new delivery/provider tests pass. The complete workspace suite passes 297 Python tests; both JavaScript checks, Django system checks and application migration consistency checks pass. Migration `0017` is applied locally.

Apply migration `0017` and restart the existing FPL worker. SMTP/SITE_URL and the shared encryption keyring must be configured; users supply their own Gemini key. No production deployment, real Google charge, inbox delivery or cross-client email visual test is claimed. See [deadline briefing delivery and operations](deadline-briefings.md) for exact timing, retries, consent and smoke checks.

## Epoch 7 — Authentication and access controls (implemented)

Security remediation is delivered in separate, tested epochs with a feedback checkpoint after each. This epoch addresses the authentication and access boundaries from the security review; the remaining work below is planned, not implemented by this epoch.

- API login requires verified email. All protected API views use the same verification permission, including previously issued tokens and existing sessions after verification is revoked. Browser login only redirects to the current host and retains a local destination through the login form.
- Tracking, untracking, logout and league creation require POST. Existing forms retain CSRF protection. Browser and API league reads/refreshes share one rule: both the league and the manager's membership must be active. Tracking limits share one transactional service, allow idempotent requests, and reactivate existing inactive records without counting inactive rivals against the allowance.
- Browser and API entry points share database-backed limits for login, signup, verification resends, password-reset requests, explicit refreshes, profile updates and manual league additions. Counters survive process/cache changes, store keyed hashes instead of raw email/IP addresses, expire automatically, and return HTTP 429 with Retry-After. These limits complement CSRF and upstream/edge protections.
- Login permits 60 attempts per client IP and 10 per normalized email per five minutes. Signup permits five per IP/hour; verification resends allow one per email/minute and five/hour, plus 20 per IP/hour. Explicit refreshes share ten requests per manager/minute across browser/API resources. On-demand score, lineup, comparison and planner computations share 30 requests per manager/minute, including GET requests. Reset requests permit ten per IP and three per email/hour; profile changes permit five per manager/five minutes and league additions ten/hour. Ordinary dashboard and squad shell page loads do not consume these limits.

Apply `core.0001_initial` before serving the new request handlers. The limiter uses `REMOTE_ADDR`; configure the application server to accept forwarded client addresses only from the trusted reverse proxy. Client-supplied forwarding headers are never trusted by application code. SQLite's immediate transactions serialize reservations; row locks provide the corresponding boundary on databases that support them. Keep edge connection/request limits in place for traffic that should be rejected before reaching Django.

Validation: 33 new security regression cases pass. The isolated epoch passes all 422 Python tests; the workspace including concurrent league-summary work passes 432 Python tests at this checkpoint. All six JavaScript tests, Django system checks and application migration consistency checks pass. A file-backed SQLite concurrency check accepted exactly three of 20 simultaneous reservations across eight connections, rejected the other 17 and reported no lock failures. Existing API ownership tests explicitly verify their actors so they continue exercising ownership rather than stopping at the new verification boundary. No production deployment or live traffic test is included.

## Epoch 8 — Deployment safeguards (implemented)

- Production runtime configuration fails closed on weak/missing Django keys, wildcard or empty allowed hosts, unsafe canonical site URLs, placeholder senders and unencrypted remote SMTP. Development, test and static-asset build paths remain usable without production credentials.
- Kamal injects SITE_URL and DEFAULT_FROM_EMAIL into both roles. Local and production environment examples distinguish development defaults from required deployment credentials. Private production credentials are not rewritten or rotated.
- Production validation is an explicit command after environment resolution, not a settings-import side effect. Existing random 44-character keys remain supported; new installations should generate strong keys. The release gate checks actual Kamal-resolved production configuration, then runs the complete Python and JavaScript suites with isolated test settings, system checks and application migration checks. Test filtering through inherited PYTEST_ADDOPTS is cleared.
- The default web command validates settings and completes migrations before Uvicorn listens, so a failed migration cannot pass the new container's health check. Migrations have been removed from the post-deploy hook. The new job container's health is checked by exact release version with a bounded wait and a nonzero result on failure.
- Tailwind v4.1.7 is pinned to official amd64/arm64 checksums and compiles assets on the native build platform, avoiding standalone compiler failures under emulation. Environment secrets, databases and SQLite sidecars are excluded from the build context. Asset collection needs no runtime secrets.

Validation: the actual pre-deploy hook passes using Kamal-resolved web/job environments, followed by all 511 Python tests and 12 JavaScript tests, system checks and application migration consistency checks. The deployment suite covers 48 cases, including compatibility with existing random 44-character keys, explicit validation without settings-import side effects, blocked startup on configuration/migration failure, and bounded worker health checks. The complete amd64 image builds with native arm64 asset compilation; a disposable non-root container rejects missing runtime settings, applies all migrations and returns HTTP 200 on /health. Build-context canaries confirm that root/nested environment files, databases and SQLite sidecars are excluded. The local ALLOWED_HOSTS and SITE_URL were aligned with the configured Kamal proxy; credentials were preserved. No remote deployment or secret rotation was performed. See [deployment safeguards](deployment.md) for configuration, boot order, build verification and rollout limits.

### Remaining security remediation epochs (planned)

- **Epoch 9 — Account and league lifecycle reliability:** make team changes atomic and stale jobs revocable; complete password recovery with uniform responses; repair missing routes/templates; validate manual league imports before persistence.
- **Epoch 10 — Bounded ingestion and maintainable snapshots:** coordinate with the ongoing league-summary work to bound ingestion/publication and paginate reads; reduce synchronous HTTP and establish shared snapshot/cache behavior; preserve historical player identity across seasons. Review the audit against the final combined implementation before closing the remediation work.

## Product ideas to prioritise through use

1. A “do this before the deadline” checklist, limited to the three highest-impact decisions, with expandable evidence.
2. A target-league mode showing the leader/nearest challengers, attainable points gap, and which squad differences matter next.
3. A fixture-swing watchlist and injury/minutes alerts tied to the user's actual squad.
4. A transfer sandbox that compares hold, one move, and two moves over several weeks with explicit cost and budget assumptions.
5. A post-gameweek review separating decision quality from luck, so users learn when the advice is useful.

Success measures: visits before deadlines, plans reviewed, advice usefulness feedback, recommendations acted on, freshness failure rate, page latency, and performance against a recorded baseline. Winning a league is the objective, but outcomes alone do not prove the tool caused the improvement.
