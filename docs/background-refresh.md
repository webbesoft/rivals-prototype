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

The first dispatcher run queues event context. Once context is available, later runs (every minute) schedule players and active users' teams, tracked rivals, and subscribed active leagues. Opening a dashboard can also queue due resources immediately. Routine ingestion imports gameweek results without fetching every historical squad. The squad carousel and tactical-analysis flow are separate work in Epoch 3.

## Refresh policy and failure behaviour

- Event context: two minutes; deadline interpretation is recalculated from stored events on every read, so crossing a known deadline does not depend on a new HTTP response.
- Profiles, history, transfers, leagues: two minutes during active fixtures, six hours before deadlines, and two hours after deadlines; thirty minutes otherwise.
- Player metadata: at least fifteen minutes between normal busy-window refreshes; thirty minutes in quiet periods.
- Gameweek or season changes make successful older snapshots due immediately.
- Failures retry after one minute, with exponential backoff capped at one hour. Manual refresh buttons respect failure backoff and existing leases.
- Bootstrap/fixtures have the existing five-minute upstream cache; the policy governs attempts rather than guaranteeing new upstream data every two minutes.

Every resource has a SyncState row containing its last successful refresh, attempt, queue state, retry time, and generation. A database reservation and task insertion occur in one transaction. Duplicate requests reuse the reservation; obsolete generations do no work. Fifteen-minute leases allow jobs abandoned by a killed worker to be reserved again. If a job finishes but its acknowledgement is lost, a later retry can safely re-import its data.

Profile/history/transfer writes are transactional after fetching the source. Failed imports keep the previous records and successful-refresh timestamp. Automatic league refreshes reject incomplete standings; leagues larger than the current API client's ten-page cap will remain in an error/retry state rather than silently present partial standings as fresh. Invalid player batches roll back. Transfers with missing player metadata display an FPL ID placeholder until a subsequent successful player refresh associates them.

## Production

`config/deploy.yml` includes a `job` role on the same host as `web`, sharing the named volume mounted at `/app/dbs`. It starts:

```sh
uv run python manage.py fpl_worker --wait-for-migrations 120
```

The job waits up to two minutes for migrations because this project's existing post-deploy hook applies them after containers boot. The hook now targets only the web role for migration and checks the worker heartbeat. If migrations take longer than the wait window, restart the job after they complete. For deployments outside this workflow, apply migrations before starting either process.

This epoch changes deployment configuration only. It does not deploy containers or assert that the remote worker is already running. After deployment, verify the running worker:

```sh
kamal app logs -r job
kamal app exec --roles web "uv run python manage.py check_fpl_worker"
```

The command exits unsuccessfully for a missing/older-than-five-minutes heartbeat or overdue leases. Retry it after the initial dispatcher run if startup has just completed. A current heartbeat plus a nonzero retry count means the worker is alive but some upstream imports are failing. The dashboard's Data freshness panel exposes the same per-resource success and retry information.

Kamal role and command configuration follows its [deployment command reference](https://kamal-deploy.org/docs/commands/deploy/) and [app command reference](https://kamal-deploy.org/docs/commands/app/).

Keep this SQLite installation on one host with one job role. More users or a backlog may require moving to a server database and revisiting worker concurrency. Scheduling intervals are targets; queue delays and FPL publishing delays affect when new data becomes visible.

## Recovery

1. Check the job logs and `check_fpl_worker` before clicking refresh repeatedly.
2. Start/restart `fpl_worker` if it is absent. Startup preserves an existing recurring dispatcher, including a locked one during rolling restarts.
3. Allow abandoned leases/task locks to expire; the dispatcher will reserve overdue resources again. A locked dispatcher abandoned during shutdown may take up to fifteen minutes to recover.
4. For upstream failures, leave saved data visible and inspect retry status. Fix any persistent payload/import error before forcing more requests.

The web and API refresh controls now enqueue work. API sync endpoints return HTTP 202 with the saved representation; clients should not interpret acceptance as completed ingestion. Web refresh controls require POST; team refreshes are limited to the user's own team or an actively tracked rival, and league refreshes require subscription.
