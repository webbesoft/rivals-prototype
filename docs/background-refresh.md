# Background refresh operations

The web process reads saved FPL data and queues due work. The worker performs HTTP requests; opening a dashboard no longer waits for rival syncs or live-season/projection lookups. Score panels retain their independent lightweight refresh from Epoch 1.

## Local startup

Install project dependencies, then apply migrations:

```sh
uv run python manage.py migrate --noinput
```

Run the web server and worker in separate terminals:

```sh
uv run python manage.py runserver
```

```sh
uv run python manage.py fpl_worker
```

`fpl_worker` seeds a recurring dispatcher and uses the installed django-background-tasks runner. It processes existing account/email tasks as well as FPL jobs. Running plain `process_tasks` without first seeding a dispatcher does not initialise recurring ingestion; use `fpl_worker` for startup.

The worker intentionally consumes tasks serially, with a fifteen-minute abandoned-lock timeout, for this shared SQLite deployment. SQLite write transactions use IMMEDIATE mode and a twenty-second busy timeout to avoid read-to-write lock upgrades. Neither the refresh scheduler nor freshness displays depend on a shared in-memory cache. Score snapshots still benefit from a shared cache when using multiple web processes.

The first dispatcher run queues event context. Once context is available, later runs (every minute) schedule players and active users' teams, tracked rivals, and subscribed active leagues. Opening a dashboard can also queue due resources immediately. Routine ingestion imports gameweek results without fetching every historical squad. Squad lineups now load one gameweek on demand; tactical briefings use a separate, short-lived cache. See Epoch 3 in the [delivery roadmap](epochs.md) for their behaviour and validation.

## Refresh policy and failure behaviour

- Event context: two minutes; deadline interpretation is recalculated from stored events on every read, so crossing a known deadline does not depend on a new HTTP response.
- Profiles, history, transfers, leagues: two minutes during active fixtures, six hours before deadlines, and two hours after deadlines; thirty minutes otherwise.
- Player metadata: at least fifteen minutes between normal busy-window refreshes; thirty minutes in quiet periods.
- Gameweek or season changes make successful older snapshots due immediately.
- Failures retry after one minute, with exponential backoff capped at one hour. Manual refresh buttons respect failure backoff and existing leases.
- Bootstrap/fixtures have the existing five-minute upstream cache; the policy governs attempts rather than guaranteeing new upstream data every two minutes.

Every resource has a SyncState row containing its last successful refresh, attempt, queue state, retry time, and generation. A database reservation and task insertion occur in one transaction. Duplicate requests reuse the reservation; obsolete generations do no work. Fifteen-minute leases allow jobs abandoned by a killed worker to be reserved again. If a job finishes but its acknowledgement is lost, a later retry can safely re-import its data.

Profile/history/transfer writes are transactional after fetching the source. Failed imports keep the previous records and successful-refresh timestamp. Automatic league refreshes fetch five standings pages per job and queue a continuation after five seconds. Each page and its next-page checkpoint are saved together in the database; worker restarts, expired leases, and upstream failures resume at the first unfinished page. There is no total page cap for automatic imports. Invalid player batches roll back. Transfers with missing player metadata display an FPL ID placeholder until a subsequent successful player refresh associates them.

League pages are staged in `LeagueImport`/`LeagueImportPage`. Published team information, memberships, ranks, league name, and successful-refresh timestamps stay unchanged until the final page has arrived, then publish in one transaction using bulk writes. Failure during publication rolls back and retains the staged pages for another attempt. A season/gameweek change or a checkpoint older than 24 hours restarts collection. Repeated entries across page boundaries trigger reconciliation: the importer re-reads affected tied-rank pages and their neighbours, accumulating unique teams until their count matches the original page slots. Each repair job fetches at most five pages and checkpoints its cursor. After three unsuccessful repair passes, it marks the staged snapshot for replacement and applies normal failure backoff. The next attempt collects from page one with a new expected count, so a changed membership count cannot trap the importer in repeated repairs of the old snapshot. Published standings remain intact throughout; it never marks a deduplicated shortfall as complete. A union larger than the original slot count indicates changed membership and restarts collection. FPL does not provide a snapshot token here: a full paginated import can contain data fetched at different times, especially during live matches. Large leagues can take many jobs to complete; five-second continuation delays also give other queued resources opportunities to run.

League failure logs include the external FPL league ID and the failed page or import reason. The `league:<id>` resource key uses the local database ID, which can differ from the FPL ID. The older synchronous API helper retains its ten-page default; automatic ingestion uses the resumable page API instead. Apply migration `0011` and restart existing workers to load the new importer.

## Production

The Docker image uses Python 3.15.2 for both build and runtime, matching `.python-version`, and installs production dependencies into `/opt/venv` during its build. The default `bin/start-web` command validates configuration and applies migrations before starting Uvicorn. Operational commands and the job role reuse the installed environment with `UV_NO_SYNC=1`; containers do not install dependencies at startup. Kamal allows 120 seconds for the new web container to become ready. After changing the Dockerfile, rebuild and redeploy the image with `kamal deploy`.

Kamal's pre-deploy test hook runs on the machine invoking Kamal and explicitly installs the interpreter pinned in `.python-version` before testing. Keep that machine's `uv` current so it knows about new Python downloads: use `brew upgrade uv` for Homebrew installations or `uv self update` for standalone installations. A Python installation failure stops deployment before tests run.

`config/deploy.yml` includes a `job` role on the same host as `web`, sharing the named volume mounted at `/app/dbs`. It starts:

```sh
uv run python manage.py fpl_worker --wait-for-migrations 120
```

The job validates resolved production configuration before its two-minute migration wait for standalone starts and rolling restarts. Under the default Kamal workflow, the web process completes migrations before becoming healthy. The post-deploy hook checks the exact deployed job container, with a 60-second bounded wait, and fails if worker health cannot be confirmed. It no longer applies migrations after traffic has switched. See [deployment safeguards](deployment.md) for required production configuration and the complete release gate. For other workflows, apply migrations before starting either process.

This epoch changes deployment configuration only. It does not deploy containers or assert that the remote worker is already running. After deployment, verify the running worker:

```sh
kamal app logs -r job
kamal app exec --roles web "uv run python manage.py check_fpl_worker"
```

The command exits unsuccessfully for a missing/older-than-five-minutes heartbeat or overdue leases. Retry it after the initial dispatcher run if startup has just completed. A current heartbeat plus a nonzero retry count means the worker is alive but some upstream imports are failing. The dashboard shows a simple notice when updates are delayed. Per-resource success and retry details remain in SyncState and operational diagnostics.

Kamal role and command configuration follows its [deployment command reference](https://kamal-deploy.org/docs/commands/deploy/) and [app command reference](https://kamal-deploy.org/docs/commands/app/).

Keep this SQLite installation on one host with one job role. More users or a backlog may require moving to a server database and revisiting worker concurrency. Scheduling intervals are targets; queue delays and FPL publishing delays affect when new data becomes visible.

## Recovery

1. Check the job logs and `check_fpl_worker` before clicking refresh repeatedly.
2. Start/restart `fpl_worker` if it is absent. Startup preserves an existing recurring dispatcher, including a locked one during rolling restarts.
3. Allow abandoned leases/task locks to expire; the dispatcher will reserve overdue resources again. A locked dispatcher abandoned during shutdown may take up to fifteen minutes to recover.
4. For upstream failures, leave saved data visible and inspect retry status. Fix any persistent payload/import error before forcing more requests.

The web and API refresh controls now enqueue work. API sync endpoints return HTTP 202 with the saved representation; clients should not interpret acceptance as completed ingestion. Web refresh controls require POST; team refreshes are limited to the user's own team or an actively tracked rival, and league refreshes require subscription.

## Large leagues

Apply migration `0020_league_summaries` and restart both web and worker processes.
`LEAGUE_FULL_IMPORT_LIMIT` defaults to 1,000 participants. Leagues above that size
use published manager ranks and shared standings pages rather than recurring full
imports. The migration marks existing oversized imports without deleting their
team or membership history. In-progress imports with unknown sizes switch when
they reach the limit and still have another page; known large leagues switch
immediately. Small leagues retain atomic full publication and resumable imports.

A profile refresh records `entry_rank`, `entry_last_rank`, points, and refresh time
per team, league, season, and gameweek. Repeated refreshes update that gameweek's
snapshot; movement comes from FPL's published previous rank, never from the last
poll. Rank zero/missing ranks are unknown. Profiles lagging the scoring gameweek
do not overwrite snapshots. Histories begin with the first successful profile
refresh after deployment, without backfilling invented positions.

Routine large-league refreshes fetch only page one. Opening a league requests the
leaders plus at most three nearby pages, or a single explicitly browsed page.
Each page is stored durably and shared across users/web processes. Nearby lookup
uses the first page's observed size (50 until available), checks for the manager's
entry, and deduplicates moving page boundaries. Ties may place the manager beyond
that bounded window; their independently published position remains visible and
users can browse further. No fallback crawls the remaining league. Tracking a rival
creates a local team only for the selected cached entry and uses normal tracking
limits and queued profile/history/transfer refreshes.

Saved page data survives upstream errors, with the normal leases and retry backoff.
The large-league screen labels stale pages and shows the rank's gameweek/update
time separately. Old full-import rows are retained for history but excluded from
large-league standings, league-wide projections, and planner league comparisons.
The API league detail response marks these results `partial: true` and
`summary_only: true`; its `standings` contain bounded FPL page rows, with separate
`leaders`, `user_rank`, `page`, `has_next`, `waiting`, and `stale` fields. Small
leagues keep the existing detail representation. Large-league projected finishing
ranks and planner league comparisons are unavailable until a method that works
with partial standings is implemented.

Only visited pages are refreshed on demand; the dispatcher never walks cached
pages. A league stays in summary mode once classified, even if its reported size
later drops. To opt a smaller league back into full ingestion, clear its
`summary_only` flag after confirming its size is within the configured limit.

Profile snapshots store season points. Leader/podium gaps are displayed only for
leagues confirmed to start at GW1, where those totals are comparable. Late-start
leagues still show their published ranks and page scores, without comparing those
scores with a manager's full-season total.
