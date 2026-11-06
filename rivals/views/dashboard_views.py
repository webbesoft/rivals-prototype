from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import models
from django.shortcuts import render

from rivals.models import GameweekData, Transfer


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

    mini_leagues_qs = request.user.customminileagues.select_related(
        "mini_league"
    ).order_by("mini_league__name")

    paginator = Paginator(mini_leagues_qs, 10)  # 10 per page
    page_number = request.GET.get("page")
    mini_leagues = paginator.get_page(page_number)
    recent_leagues = mini_leagues[:5]
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
        # Use a short-lived negative cache so a failed lookup does not trigger a
        # fresh FPL API request on every dashboard load.
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
        },
    )


def fetch_recent_activity(user):
    team_ids = user.tracked_teams.values_list("team_id", flat=True)

    return {
        "transfers": Transfer.objects.filter(team_id__in=team_ids)
        .select_related("team", "player_in", "player_out")
        .order_by("-transfer_time", "-id")[:10],
        "gameweek_data": GameweekData.objects.filter(team_id__in=team_ids)
        .select_related("team")
        .order_by(
            # Current-season rows first (nulls last), then most recent gameweek
            models.F("season").desc(nulls_last=True),
            "-gameweek",
            "-id",
        )[:10],
    }
