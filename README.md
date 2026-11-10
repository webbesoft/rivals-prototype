# Details

This was the original prototype for [Rivals](https://rivals.webbe.dev) that I did in Django.

It had feature parity as of 23/09/2024. However, I will not add any more features to this version as I prefer working with Laravel (nothing wrong with Django, just preference).

This version can be self-hosted via the included Dockerfile for anyone who just wants to tinker with this without registering on my hosted on version.

The architecture is based on my Django starter, with a separate `accounts` app for all user authentication functionality. With a few tweaks, this (along with the `rivalspy` Django project) can be extracted and used as a starting point for a decently functional web application.

## Stack

- Django
- [Django-cotton](https://django-cotton.com/)
- SQLite
- TailwindCSS via the standalone binary


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

See [refresh operations](docs/background-refresh.md) and the [delivery epochs](docs/epochs.md). The current implementation is being developed beyond the original prototype described above.
