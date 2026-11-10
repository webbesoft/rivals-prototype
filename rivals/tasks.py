import logging

from background_task import background
from django.db import transaction
from django.utils import timezone

from rivals.models import MiniLeague, SyncState, Team, TrackedTeam, UserMiniLeague
from rivals.services.fpl_api_service import FplApiService
from rivals.services.gameweek_context_service import GameweekContextService

logger = logging.getLogger(__name__)


@background(schedule=0)
def refresh_resource(key, generation):
    now = timezone.now()
    claimed = SyncState.objects.filter(
        key=key, generation=generation, status="queued"
    ).update(
        status="running",
        last_attempt_at=now,
        lease_until=now + timezone.timedelta(minutes=15),
    )
    if not claimed:
        return
    state = SyncState.objects.get(key=key)
    try:
        payload = ingest(state)
        if payload is None:
            # Checkpoint and continuation are separate from successful publication.
            # Queue insertion and lease handoff commit together.
            with transaction.atomic():
                continued = SyncState.objects.filter(
                    pk=state.pk, generation=generation, status="running"
                ).update(
                    status="queued",
                    generation=generation + 1,
                    failures=0,
                    error="",
                    next_attempt_at=None,
                    lease_until=timezone.now() + timezone.timedelta(minutes=15),
                )
                if continued:
                    refresh_resource(key, generation + 1, schedule=5)
            return
    except Exception:
        # A bounded, generic error is sufficient for users; detailed trace stays in logs.
        logger.exception("FPL refresh failed for %s", key)
        failures = state.failures + 1
        SyncState.objects.filter(pk=state.pk, generation=generation).update(
            status="error",
            failures=failures,
            error="Refresh failed; saved data retained.",
            next_attempt_at=timezone.now()
            + timezone.timedelta(seconds=min(3600, 60 * 2 ** min(failures - 1, 6))),
            lease_until=None,
        )
        return
    context = (
        GameweekContextService.derive(payload["events"], payload["fixtures"])
        if state.kind == "context"
        else GameweekContextService.cached()
    )
    payload.setdefault(
        "stamp", f'{context.get("season")}:{context.get("scoring_gameweek")}'
    )
    SyncState.objects.filter(pk=state.pk, generation=generation).update(
        status="ok",
        last_success_at=timezone.now(),
        failures=0,
        error="",
        next_attempt_at=None,
        lease_until=None,
        payload=payload,
    )


def ingest(state):
    api = FplApiService()
    api.TIMEOUT = 5
    if state.kind == "context":
        bootstrap = api.fetch_bootstrap_static()
        if not bootstrap or not bootstrap.get("events"):
            raise RuntimeError("FPL events unavailable")
        fixtures = api.fetch_fixtures_data()
        if fixtures is None:
            raise RuntimeError("FPL fixtures unavailable")
        context = GameweekContextService.derive(bootstrap["events"], fixtures)
        if not context["season"]:
            raise RuntimeError("FPL deadlines unavailable")
        from django.core.cache import cache

        cache.set(api.SEASON_CACHE_KEY, context["season"], api.SEASON_CACHE_TIMEOUT)
        return {"events": bootstrap["events"], "fixtures": fixtures}
    context = GameweekContextService.cached()
    if context["stale"] or not context["season"]:
        raise RuntimeError("Fresh season context required before ingestion")
    if state.kind == "decisions":
        from rivals.services.decision_review_service import review_decisions

        return {"reviewed": review_decisions(api)}
    if state.kind == "players":
        from rivals.services.player_sync_service import PlayerSyncService

        result = PlayerSyncService(api).sync_all_players()
        if not result["success"]:
            raise RuntimeError("Player import failed")
        from rivals.services.transfer_player_association_service import (
            TransferPlayerAssociationService,
        )

        TransferPlayerAssociationService().associate_all_transfers(
            season=context["season"]
        )
    elif state.kind == "league":
        from rivals.services.league_import_service import LeagueImportService

        service = LeagueImportService(state, api, context)
        try:
            complete = service.run_batch()
        except Exception as exc:
            league = MiniLeague.objects.get(pk=state.object_id)
            raise RuntimeError(
                f"League import failed (FPL league {league.fpl_league_id}): {exc}"
            ) from exc
        if not complete:
            return None
        return {"stamp": service.stamp}
    else:
        from rivals.services.team_sync_service import TeamSyncService

        team = Team.objects.get(pk=state.object_id)
        service = TeamSyncService(team)
        service.fpl_api_service = api
        # Fetch before opening a write transaction, retaining saved data on failure.
        if state.kind == "profile":
            data = api.fetch_team_basic_info(team.fpl_team_id)
            if not data:
                raise RuntimeError("Profile unavailable")
        elif state.kind == "history":
            data = api.fetch_team_history(team.fpl_team_id)
            if not data or "current" not in data:
                raise RuntimeError("History unavailable")
            service._history_data = data
        else:
            data = api.fetch_team_transfers(team.fpl_team_id)
            if data is None:
                raise RuntimeError("Transfers unavailable")
        with transaction.atomic():
            service.detect_and_handle_season_transition(live_season=context["season"])
            if state.kind == "profile":
                success = service.sync_basic_info(data=data)
            elif state.kind == "history":
                success = service.sync_history(include_picks=False) is not None
            else:
                success = service.sync_transfers(
                    transfers=data, sync_missing_players=False
                )
            if not success:
                raise RuntimeError("Team import failed")

    return {}


@background(schedule=0)
def refresh_tick():
    """One recurring dispatcher; normal tasks perform the network work."""
    from django.contrib.auth import get_user_model

    from rivals.services.refresh_service import enqueue_refresh, enqueue_team

    SyncState.objects.update_or_create(
        key="worker",
        defaults={
            "kind": "worker",
            "status": "ok",
            "last_success_at": timezone.now(),
        },
    )
    context = GameweekContextService.cached()
    enqueue_refresh("context", context=context)
    from rivals.services.deadline_digest_service import dispatch_digests

    dispatch_digests()
    if context["stale"]:
        return
    enqueue_refresh("players", context=context)
    enqueue_refresh("decisions", context=context)
    team_ids = set(
        get_user_model()
        .objects.filter(is_active=True, team__active=True)
        .values_list("team_id", flat=True)
    )
    team_ids.update(
        TrackedTeam.objects.filter(
            user__is_active=True, team__active=True, active=True
        ).values_list("team_id", flat=True)
    )
    for team_id in sorted(team_ids):
        enqueue_team(team_id, context=context)
    league_ids = (
        UserMiniLeague.objects.filter(
            active=True, mini_league__active=True, user__is_active=True
        )
        .values_list("mini_league_id", flat=True)
        .distinct()
    )
    for league_id in league_ids:
        enqueue_refresh("league", league_id, context=context)


# Register reference-only Gemini work with the existing task worker.
from rivals.services.ai_briefing_service import prepare_briefing  # noqa: E402, F401
from rivals.services.deadline_digest_service import (
    generate_digest,
    deliver_digest,
)  # noqa: E402, F401
