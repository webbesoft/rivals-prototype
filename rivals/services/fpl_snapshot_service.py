"""Cross-process FPL publications, owned by the durable ingestion lease."""

from django.utils import timezone

from rivals.models import FplSnapshot, SquadRefresh, Team


def read_snapshot(key):
    row = FplSnapshot.objects.filter(key=key, expires_at__gt=timezone.now()).first()
    return row.payload if row else None


def publish_snapshots(snapshots):
    now = timezone.now()
    for key, payload in snapshots.items():
        ttl = (
            900
            if key in ("bootstrap", "fixtures")
            else 3600 if key.startswith(("profile:", "history:")) else 120
        )
        FplSnapshot.objects.update_or_create(
            key=key,
            defaults={
                "payload": payload,
                "updated_at": now,
                "expires_at": now + timezone.timedelta(seconds=ttl),
            },
        )
    # Bound obsolete-season/event publications without clearing fresh readers.
    expired = list(
        FplSnapshot.objects.filter(
            expires_at__lt=now - timezone.timedelta(days=2)
        ).values_list("pk", flat=True)[:100]
    )
    FplSnapshot.objects.filter(pk__in=expired).delete()


def enqueue_squad(team, season, gameweek):
    from .gameweek_context_service import GameweekContextService
    from .refresh_service import enqueue_refresh

    context = GameweekContextService.cached()
    if context["stale"]:
        enqueue_refresh("context", context=context)
    if (
        not season
        or season != context["season"]
        or type(gameweek) is not int
        or not 1 <= gameweek <= (context["scoring_gameweek"] or 0)
    ):
        return False
    target, _ = SquadRefresh.objects.get_or_create(
        team=team, season=season, gameweek=gameweek
    )
    return enqueue_refresh("squad", target.pk, context=context)


def request_snapshot(key):
    from .gameweek_context_service import GameweekContextService
    from .refresh_service import enqueue_refresh

    if key in ("bootstrap", "fixtures"):
        enqueue_refresh("context", force=True)
    elif key.startswith("picks:"):
        _, season, entry, week = key.split(":")
        team = Team.objects.filter(fpl_team_id=entry).first()
        if team:
            enqueue_squad(team, season, int(week))

    elif key.startswith(("profile:", "history:")):
        kind, season, entry = key.split(":")
        team = Team.objects.filter(fpl_team_id=entry).first()
        if team and season == GameweekContextService.cached()["season"]:
            enqueue_refresh(kind, team.pk, force=True)
