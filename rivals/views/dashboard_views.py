import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import models
from django.shortcuts import redirect, render
from django.utils import timezone

from rivals.models import GameweekData, Transfer
from rivals.services.score_summary_service import ScoreSummaryService

logger = logging.getLogger(__name__)


@login_required
def dashboard(request):
    # Self-heal user.team link if user has an fpl_id but team became unlinked
    if request.user.team_id is None and request.user.fpl_id:
        from rivals.models import Team

        matched_team = Team.objects.filter(fpl_team_id=request.user.fpl_id).first()
        if matched_team:
            request.user.team = matched_team
            request.user.save(update_fields=["team"])

    user_team = request.user.team

    # Auto-sync tracked rivals if a gameweek deadline has passed
    sync_tracked_rivals_if_deadline_passed(request.user)

    # Fetch user's custom mini leagues, filtering active entries only
    all_user_leagues = list(
        request.user.customminileagues.filter(
            active=True,
            mini_league__active=True,
        )
        .select_related("mini_league")
        .order_by("mini_league__name", "-mini_league__last_synced_at", "-id")
    )

    # Filter out previous season leagues and eliminate duplicates
    filtered_leagues = []
    seen_league_names = set()
    seen_fpl_ids = set()

    for uml in all_user_leagues:
        ml = uml.mini_league
        # Check if this league is strictly from a previous season
        if user_team and user_team.current_season:
            user_mlt = ml.mini_league_teams.filter(team=user_team).first()
            if (
                user_mlt
                and user_mlt.season
                and user_mlt.season != user_team.current_season
            ):
                # Obsolete league from an older season; mark inactive to prune
                uml.active = False
                uml.save(update_fields=["active"])
                continue

        # Deduplicate: if duplicate name or fpl_league_id
        league_key = ml.name.strip().lower()
        if ml.fpl_league_id in seen_fpl_ids or league_key in seen_league_names:
            continue

        seen_league_names.add(league_key)
        seen_fpl_ids.add(ml.fpl_league_id)
        filtered_leagues.append(uml)

    paginator = Paginator(filtered_leagues, 10)  # 10 per page
    page_number = request.GET.get("page")
    mini_leagues = paginator.get_page(page_number)
    recent_leagues = filtered_leagues[:5]

    tracked_teams_qs = request.user.tracked_teams.select_related("team").all()
    tracked_teams_count = tracked_teams_qs.count()
    tracked_teams = tracked_teams_qs[:5]
    recent_activity = fetch_recent_activity(request.user)

    user_gameweek_data = None
    new_season_detected = False
    user_projection = None
    mini_league_projections = []

    if request.user.team_id:
        from rivals.services.projection_service import ProjectionService

        projection_service = ProjectionService()
        user_projection = projection_service.project_team_finish(user_team)

        projections_by_id = {}
        for uml in mini_leagues:
            ml_proj = projection_service.get_team_mini_league_summary(
                user_team, uml.mini_league
            )
            if ml_proj:
                mini_league_projections.append(ml_proj)
                projections_by_id[uml.mini_league_id] = ml_proj
                uml.projection = ml_proj

        gw_qs = request.user.team.gameweek_data.filter(rank__isnull=False)
        if user_team and user_team.current_season:
            gw_qs = gw_qs.filter(season=user_team.current_season)
        user_gameweek_data = gw_qs.order_by("gameweek")

        # Check if FPL API reports a newer season than the user team's active season.
        from rivals.services.fpl_api_service import FplApiService

        _SEASON_MISS_KEY = "dashboard_season_lookup_failed"
        if not cache.get(_SEASON_MISS_KEY):
            live_season = FplApiService().fetch_current_season()
            if live_season is None:
                # Cache the failure for 60 s to avoid hammering the API
                cache.set(_SEASON_MISS_KEY, True, 60)
        else:
            live_season = None
        if (
            live_season
            and user_team
            and user_team.current_season
            and live_season != user_team.current_season
        ):
            new_season_detected = True

    return render(
        request,
        "pages/dashboard.html",
        {
            "mini_leagues": mini_leagues,
            "user_team": user_team,
            "user_projection": user_projection,
            "mini_league_projections": mini_league_projections,
            "recent_leagues": recent_leagues,
            "tracked_teams": tracked_teams,
            "tracked_teams_count": tracked_teams_count,
            "recent_activity": recent_activity,
            "user_gameweek_data": user_gameweek_data,
            "new_season_detected": new_season_detected,
            "score_summary": (
                ScoreSummaryService(user_team).cached() if user_team else None
            ),
        },
    )


def sync_tracked_rivals_if_deadline_passed(user):
    """
    If the latest gameweek deadline has passed, ensure tracked rivals have
    their transfers updated as soon as data becomes available.
    """
    from django.conf import settings

    if getattr(settings, "TESTING", False):
        return

    tracked_qs = user.tracked_teams.select_related("team").all()
    if not tracked_qs.exists():
        return

    from rivals.services.fpl_api_service import FplApiService
    from rivals.services.team_sync_service import TeamSyncService

    api = FplApiService()
    latest_gw, deadline_dt = api.get_latest_passed_deadline()
    if not deadline_dt:
        return

    for tracked in tracked_qs:
        team = tracked.team
        # If team has never been synced or was last synced before the gameweek deadline passed
        if team.last_synced_at is None or team.last_synced_at < deadline_dt:
            throttle_key = f"sync_rival_throttle_{team.id}_{latest_gw}"
            if not cache.get(throttle_key):
                cache.set(throttle_key, True, 300)  # 5 minute throttle
                try:
                    sync_service = TeamSyncService(team)
                    sync_service.sync_transfers()
                    team.last_synced_at = timezone.now()
                    team.save(update_fields=["last_synced_at"])
                except Exception as e:
                    logger.warning(f"Auto-sync for tracked rival {team.id} failed: {e}")


@login_required
def dashboard_sync_rivals(request):
    """Manual sync action for tracked rivals in Rival Radar."""
    from rivals.services.team_sync_service import TeamSyncService

    tracked_teams = request.user.tracked_teams.select_related("team").all()
    count = 0
    for tracked in tracked_teams:
        try:
            sync_service = TeamSyncService(tracked.team)
            sync_service.sync_transfers()
            tracked.team.last_synced_at = timezone.now()
            tracked.team.save(update_fields=["last_synced_at"])
            count += 1
        except Exception as e:
            logger.warning(f"Manual sync for rival {tracked.team_id} failed: {e}")

    if count > 0:
        messages.success(
            request,
            f"Successfully updated transfers & intel for {count} tracked rival(s)!",
        )
    else:
        messages.info(request, "No tracked rivals found to synchronize.")

    return redirect("rivals:dashboard")


def fetch_recent_activity(user):
    team_ids = user.tracked_teams.values_list("team_id", flat=True)

    return {
        "transfers": Transfer.objects.filter(team_id__in=team_ids)
        .select_related("team", "player_in", "player_out")
        .order_by("-transfer_time", "-id")[:10],
        "gameweek_data": GameweekData.objects.filter(team_id__in=team_ids)
        .select_related("team")
        .order_by(
            models.F("season").desc(nulls_last=True),
            "-gameweek",
            "-id",
        )[:10],
    }
