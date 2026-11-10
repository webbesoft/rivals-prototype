import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from rivals.models import GameweekData, Transfer
from rivals.services.gameweek_context_service import GameweekContextService
from rivals.services.refresh_service import (
    enqueue_refresh,
    enqueue_user,
    refresh_status,
)
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

    # Queue only: page rendering must never call FPL.
    enqueue_user(request.user)
    gameweek_context = GameweekContextService.cached()

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

    tracked_teams_qs = request.user.tracked_teams.filter(active=True).select_related(
        "team"
    )
    tracked_teams_count = tracked_teams_qs.count()
    tracked_teams = tracked_teams_qs[:5]
    recent_activity = fetch_recent_activity(request.user, request.GET, gameweek_context)

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

        live_season = gameweek_context["season"]
        new_season_detected = bool(
            live_season
            and user_team.current_season
            and live_season != user_team.current_season
        )

    return render(
        request,
        "pages/dashboard.html",
        {
            **refresh_status(request.user),
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


@login_required
@require_POST
def dashboard_sync_rivals(request):
    for team_id in request.user.tracked_teams.filter(active=True).values_list(
        "team_id", flat=True
    ):
        for kind in ("profile", "history", "transfers"):
            enqueue_refresh(kind, team_id, force=True)
    messages.info(request, "Rival updates requested.")
    return redirect("rivals:dashboard")


@login_required
@never_cache
def dashboard_refresh_status(request):
    return render(request, "partials/refresh_status.html", refresh_status(request.user))


@login_required
@never_cache
def dashboard_intel(request):
    return render(
        request,
        "partials/rival_intel.html",
        {
            "recent_activity": fetch_recent_activity(request.user, request.GET),
        },
    )


def fetch_recent_activity(user, params=None, context=None):
    params = params or {}
    context = context if context is not None else GameweekContextService.cached()
    team_ids = user.tracked_teams.filter(active=True).values_list("team_id", flat=True)
    transfers = Transfer.objects.filter(team_id__in=team_ids)
    scores = GameweekData.objects.filter(team_id__in=team_ids)
    seasons = sorted(
        set(transfers.exclude(season__isnull=True).values_list("season", flat=True))
        | set(scores.exclude(season__isnull=True).values_list("season", flat=True)),
        reverse=True,
    )
    default_season = (
        context["season"]
        or (user.team.current_season if user.team else None)
        or (seasons[0] if seasons else None)
    )
    season = params.get("season") or default_season
    if season != "all":
        # With unknown context and no labelled data, only show unlabelled rows.
        transfers = transfers.filter(season=season)
        scores = scores.filter(season=season)
    try:
        gameweek = int(params.get("gameweek", ""))
        if not 1 <= gameweek <= 38:
            gameweek = None
    except (TypeError, ValueError):
        gameweek = None
    if gameweek:
        transfers = transfers.filter(gameweek=gameweek)
        scores = scores.filter(gameweek=gameweek)
    if default_season and default_season not in seasons:
        seasons.insert(0, default_season)
    return {
        "transfers": Paginator(
            transfers.select_related("team", "player_in", "player_out").order_by(
                "-season", "-gameweek", "-transfer_time", "-id"
            ),
            10,
        ).get_page(params.get("transfers_page")),
        "gameweek_data": Paginator(
            scores.select_related("team").order_by("-season", "-gameweek", "-id"), 10
        ).get_page(params.get("scores_page")),
        "seasons": seasons,
        "selected_season": season,
        "selected_gameweek": gameweek,
    }
