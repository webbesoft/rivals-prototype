"""Database-backed scheduling, leases, and freshness policies for FPL ingestion."""

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from rivals.models import SyncState, Team
from rivals.services.gameweek_context_service import GameweekContextService

KINDS = {
    "context",
    "players",
    "profile",
    "history",
    "transfers",
    "league",
    "league_page",
    "decisions",
    "squad",
}


def resource_key(kind, object_id=None):
    if kind not in KINDS:
        raise ValueError("Unknown refresh kind")
    return f"{kind}:{object_id}" if object_id is not None else kind


def _skip_refresh(state, kind, context, now, force):
    if state.lease_until and state.lease_until > now:
        return True
    if state.next_attempt_at and state.next_attempt_at > now and state.failures:
        return True
    stamp = f'{context.get("season")}:{context.get("scoring_gameweek")}'
    rolled_over = bool(context.get("season") and state.payload.get("stamp") != stamp)
    interval = 120 if kind == "context" else context.get("refresh_seconds", 1800)
    if kind == "squad":
        interval = 120
    if kind == "players":
        interval = max(interval, 900)
    return bool(
        not force
        and not rolled_over
        and state.last_success_at
        and (now - state.last_success_at).total_seconds() < interval
    )


def enqueue_refresh(kind, object_id=None, force=False, context=None):
    """Atomically reserve and enqueue; no HTTP and no cache dependency."""
    from rivals.tasks import refresh_resource

    context = context if context is not None else GameweekContextService.cached()
    now = timezone.now()
    key = resource_key(kind, object_id)
    # Most page loads only discover fresh or already-queued work. Avoid taking
    # SQLite's write lock for those reads. Recheck inside the transaction below.
    existing = SyncState.objects.filter(key=key).first()
    if existing and _skip_refresh(existing, kind, context, now, force):
        return False
    with transaction.atomic():
        state, _ = SyncState.objects.select_for_update().get_or_create(
            key=key, defaults={"kind": kind, "object_id": object_id}
        )
        if _skip_refresh(state, kind, context, now, force):
            return False
        claimed = (
            SyncState.objects.filter(pk=state.pk, generation=state.generation)
            .filter(Q(lease_until__isnull=True) | Q(lease_until__lte=now))
            .update(
                status="queued",
                generation=F("generation") + 1,
                lease_until=now + timezone.timedelta(minutes=15),
            )
        )
        if not claimed:
            return False
        refresh_resource(
            key, state.generation + 1, priority=9 if kind == "context" else 0
        )
    return True


def enqueue_team(team_id, force=False, context=None):
    for kind in ("profile", "history", "transfers"):
        enqueue_refresh(kind, team_id, force=force, context=context)
    if force:
        from .fpl_snapshot_service import enqueue_squad

        context = context if context is not None else GameweekContextService.cached()
        team = Team.objects.get(pk=team_id)
        enqueue_squad(team, context["season"], context["scoring_gameweek"], force=True)


def enqueue_user(user, force=False):
    context = GameweekContextService.cached()
    resources = [("context", None), ("players", None)]
    team_ids = set(
        user.tracked_teams.filter(active=True).values_list("team_id", flat=True)
    )
    if user.team_id:
        team_ids.add(user.team_id)
    for team_id in sorted(team_ids):
        resources.extend(
            (kind, team_id) for kind in ("profile", "history", "transfers")
        )
    for league_id in user.customminileagues.filter(
        active=True, mini_league__active=True
    ).values_list("mini_league_id", flat=True):
        resources.append(("league", league_id))
    # A warm page load checks freshness in one read, regardless of rival count.
    # Due resources still recheck/claim atomically in enqueue_refresh.
    existing = SyncState.objects.in_bulk(
        [resource_key(kind, pk) for kind, pk in resources], field_name="key"
    )
    now = timezone.now()
    for kind, pk in resources:
        state = existing.get(resource_key(kind, pk))
        resource_force = force and kind not in ("context", "players")
        if state and _skip_refresh(state, kind, context, now, resource_force):
            continue
        enqueue_refresh(kind, pk, force=resource_force, context=context)


def refresh_status(user):
    team_ids = set(
        user.tracked_teams.filter(active=True).values_list("team_id", flat=True)
    )
    if user.team_id:
        team_ids.add(user.team_id)
    keys = ["context", "players"]
    keys += [
        resource_key(kind, pk)
        for pk in team_ids
        for kind in ("profile", "history", "transfers")
    ]
    keys += [
        resource_key("league", pk)
        for pk in user.customminileagues.filter(active=True).values_list(
            "mini_league_id", flat=True
        )
    ]
    states = list(SyncState.objects.filter(key__in=keys).order_by("kind", "object_id"))
    names = dict(Team.objects.filter(pk__in=team_ids).values_list("pk", "team_name"))
    for state in states:
        state.label = (
            f"{names.get(state.object_id, 'Team')} {state.kind}"
            if state.kind in ("profile", "history", "transfers")
            else state.kind.title()
        )
    heartbeat = SyncState.objects.filter(key="worker").first()
    return {
        "refresh_states": states,
        "worker_healthy": bool(
            heartbeat
            and heartbeat.last_success_at
            and (timezone.now() - heartbeat.last_success_at).total_seconds() < 300
        ),
        "gameweek_context": GameweekContextService.cached(),
    }
