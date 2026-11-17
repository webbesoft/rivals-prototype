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
