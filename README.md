# Details

My Fantasy Premier League rivals tracker. Now, with AI features.

## Stack

- Django
- [Django-cotton](https://django-cotton.com/)
- SQLite
- TailwindCSS via the standalone binary


## Deployment

See [deployment safeguards](docs/deployment.md) for production environment requirements and the release gate. The default Docker web command validates configuration and completes migrations before accepting traffic. Start from `.env.production.example` when preparing deployment settings.

Google and email-link sign-in are available behind `PASSWORDLESS_AUTH_ENABLED`, with required FPL onboarding before dashboard access. See [authentication rollout](docs/authentication.md) for configuration, account migration and API changes.

## Progressive web app

RivalSpy supports browser installation and a connection message when opened
offline. See [PWA behavior and verification](docs/pwa.md) for installation,
caching policy and deployment checks.

## AI assistant connections

The **MCP** tab in AI settings connects an assistant to read your published Rivals data. See [MCP setup and operations](docs/mcp.md) for ASGI hosting, account linking, configuration and verification.

## Automatic FPL refreshes

After installing dependencies, apply migrations and run both processes:

```sh
uv run python manage.py migrate --noinput
uv run python manage.py runserver
# In a second terminal:
uv run python manage.py fpl_worker
```

The worker refreshes players, subscribed leagues, your team, and tracked rivals without manual sync clicks. Check it with `uv run python manage.py check_fpl_worker`.

The worker automatically reconciles league memberships for each active account
on its next successful profile refresh. It adds current FPL leagues and deactivates
missing account imports, including older unclassified memberships. Historical
standings and memberships explicitly marked as manually added are retained.
Failed or incomplete FPL responses never retire memberships. No per-user cleanup
command is required after deployment or season changes.

For troubleshooting, an optional command previews the same legacy cleanup:

```sh
uv run python manage.py reconcile_fpl_leagues --user-id 1 --retire-legacy
# After reviewing the dry run:
uv run python manage.py reconcile_fpl_leagues --user-id 1 --retire-legacy --apply
```

SQLite uses WAL mode on the shared local volume so atomic league imports do not
block session and standings reads. Publication remains atomic; concurrent writers
still serialize. Initialization queues network work after its database transaction.

The transfer planner loads progressively: the unchanged-squad forecast appears
before transfer comparisons, and editing controls load when opened. Rival choices
are limited to 50 matches with search. Planner fragments support gzip compression
and expose a `Server-Timing` header in browser Network tools; responses taking over
10 seconds on the server are logged as `Slow planner response`.

See [refresh operations](docs/background-refresh.md) and the [delivery epochs](docs/epochs.md). The current implementation is being developed beyond the original prototype described above.

## Achievements

Each team's **Achievements** tab keeps a seasonal trophy cabinet for 100/125/150-point
gameweeks, consecutive overall-rank improvements, captain hauls before multiplication,
above-average streaks, private classic league leadership and season championships.
Scores qualify before transfer-hit deductions; chips count, and inherited
vice-captaincy qualifies. Official tied-first league positions count when at least
two entrants are confirmed. League trophies are visible only to viewers with access
to that league.

Apply migrations through `0026_achievements` and restart the existing `fpl_worker`.
The worker quietly backfills the current season, fetching at most two historical
lineups per team per dispatcher pass. It waits for finished, data-checked events
and results refreshed afterward. Verified corrections reconcile existing awards;
missing data never extends a streak. Settled captain results are rechecked daily.
The dashboard groups newly earned badges and supports per-user dismissal.
Refresh pages to see completed background work. The `achievements:<team_id>`
`SyncState` records expose evaluation status, missing lineups and streak progress
alongside existing refresh diagnostics. Older seasons are retained, without
inferring unsupported awards from past-season totals.

## Forecasts and accuracy

The planner and public Best XI share a player-points model. The worker saves
pre-deadline forecasts and scores their frozen XI/captain against final results
and a hindsight benchmark. Apply migrations through `0024` before starting it.
See [the current formula](docs/gameweek-predictions.md) and
[model evaluation and experimental challengers](docs/decision-engine.md) for
assumptions, historical backtesting and reporting commands.

The [component validation results](docs/component-validation-results.md) compare
the challenger on a fresh historical season and isolate the effects of fixtures,
underlying stats, minutes and missing histories. Run `validate_components` to
repeat the study; the challenger remains in shadow mode.

The [calibration and history follow-up](docs/component-calibration-results.md)
adds a versioned shadow revision and candidate-prioritised history refreshes.
`check_forecast_history` reports input coverage without writing; optional bounded
warmup and capture flags are documented there. Restart the FPL worker after deploying
the revision so future captures use experiment v4.

To use the revised calculations in the UI, set
`FPL_PLAYER_MODEL=decision-v12-calibrated` and deploy both roles with `kamal deploy`.
See [activation instructions](docs/deployment.md#using-the-revised-calculations-in-the-ui).
