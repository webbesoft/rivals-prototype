from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from rivals.forms import PlanningInputForm
from rivals.models import Player, Team, TeamPlanningInput
from rivals.services.gameweek_context_service import GameweekContextService
from rivals.services.projection_service import ProjectionService
from rivals.services.refresh_service import enqueue_team, refresh_status
from rivals.services.score_summary_service import ScoreSummaryService
from rivals.services.squad_history_service import SquadHistoryService
from rivals.services.team_analysis_service import TeamAnalysisService
from rivals.services.team_sync_service import TeamSyncService


@login_required
@require_http_methods(["GET"])
@never_cache
def team_score(request, team_id):
    team = get_object_or_404(Team, id=team_id)
    return render(
        request,
        "partials/score_summary.html",
        {"score_summary": ScoreSummaryService(team).refresh()},
    )


@login_required
def team_show(request, team_id):
    team = get_object_or_404(Team, id=team_id)

    enqueue_team(team.id)
    gw_qs = team.gameweek_data
    if team.current_season:
        gw_qs = gw_qs.filter(season=team.current_season)
    gameweek_data = gw_qs.order_by("-gameweek")[:10]
    transfers = team.transfers.select_related("player_in", "player_out").order_by(
        "-gameweek"
    )[:20]

    mini_league_performance = calculate_mini_league_performance(team)
    recent_news = get_recent_player_news(transfers)
    season_histories = team.season_histories.all()
    projection = ProjectionService().project_team_finish(team)

    selection = SquadHistoryService(team).selection()

    return render(
        request,
        "pages/teams/show.html",
        {
            **refresh_status(request.user),
            "team": team,
            "gameweek_data": gameweek_data,
            "transfers": transfers,
            "mini_league_performance": mini_league_performance,
            "recent_news": recent_news,
            "season_histories": season_histories,
            "projection": projection,
            "squad_selection": selection,
            "score_summary": ScoreSummaryService(team).cached(),
        },
    )


@login_required
def team_track(request, team_id):
    team = get_object_or_404(Team, id=team_id)

    if not request.user.can_track_team:
        messages.error(
            request, "Free plan allows tracking 1 team only. Upgrade to premium."
        )
        return redirect("pricing")

    request.user.tracked_teams.get_or_create(team=team)

    enqueue_team(team.id, force=True)
    messages.success(request, "Team is now being tracked.")

    return redirect("rivals:teams.show", team_id=team.id)


@login_required
def team_untrack(request, team_id):
    team = get_object_or_404(Team, id=team_id)
    request.user.tracked_teams.filter(team=team).delete()
    messages.success(request, "Team untracked successfully!")

    return redirect("rivals:teams.show", team_id=team.id)


@login_required
@require_POST
def team_sync(request, team_id):
    team = get_object_or_404(Team, id=team_id)
    if (
        request.user.team_id != team.id
        and not request.user.tracked_teams.filter(team=team, active=True).exists()
    ):
        return JsonResponse(
            {"error": "Track this team before requesting a refresh."}, status=403
        )
    enqueue_team(team.id, force=True)
    messages.info(
        request,
        "Team update requested.",
    )
    return redirect("rivals:teams.show", team_id=team.id)


def calculate_mini_league_performance(team):
    from collections import defaultdict

    from rivals.models import MiniLeagueTeam

    mlts = list(team.mini_league_teams.select_related("mini_league").all())
    if not mlts:
        return []

    league_ids = [mlt.mini_league_id for mlt in mlts]
    all_standings = MiniLeagueTeam.objects.filter(mini_league_id__in=league_ids).values(
        "mini_league_id", "current_rank"
    )

    league_ranks = defaultdict(list)
    for s in all_standings:
        if s["current_rank"] is not None:
            league_ranks[s["mini_league_id"]].append(s["current_rank"])

    results = []
    for mlt in mlts:
        ranks = league_ranks.get(mlt.mini_league_id, [])
        total_teams = len(ranks)
        better_ranks = (
            sum(1 for r in ranks if r <= mlt.current_rank)
            if mlt.current_rank is not None
            else 0
        )

        results.append(
            {
                "league": mlt.mini_league,
                "current_rank": mlt.current_rank,
                "previous_rank": mlt.previous_rank,
                "rank_change": mlt.rank_change,
                "total_points": mlt.total_points,
                "percentile": (
                    (better_ranks / total_teams * 100) if total_teams > 0 else 0
                ),
            }
        )

    return sorted(
        results,
        key=lambda r: (r["current_rank"] is None, r["current_rank"] or 0),
    )


def get_recent_player_news(transfers):
    transfer_list = list(transfers[:5])
    if not transfer_list:
        return []

    recent_player_ids = set()
    for t in transfer_list:
        if t.player_in_id:
            recent_player_ids.add(t.player_in_id)
        if t.player_out_id:
            recent_player_ids.add(t.player_out_id)

    return list(
        Player.objects.filter(
            id__in=recent_player_ids,
            news__isnull=False,
        )
        .exclude(news="")
        .filter(news_added__gt=timezone.now() - timezone.timedelta(days=7))
        .order_by("-news_added")[:3]
    )


def planning_input(request, team):
    context = GameweekContextService.cached()
    saved = TeamPlanningInput.objects.filter(
        user=request.user,
        team=team,
        season=context["season"],
        gameweek=context["decision_gameweek"],
    ).first()
    value = saved.free_transfers if saved else None
    form = PlanningInputForm(
        request.POST if request.method == "POST" else None,
        initial={"free_transfers": value},
    )
    return form, value


@login_required
@require_http_methods(["GET"])
@never_cache
def get_squad_history(request, team_id, gameweek):
    team = get_object_or_404(Team, id=team_id)
    service = SquadHistoryService(team)
    try:
        selected = service.selection(request.GET.get("season"), gameweek)
    except ValueError as error:
        return JsonResponse({"error": str(error)}, status=400)
    try:
        rows, error = service.load(selected["season"], selected["gameweek"])
    except Exception:
        import logging

        logging.getLogger(__name__).exception("Squad unavailable for team %s", team.pk)
        return JsonResponse(
            {"error": "We couldn't load this lineup. Please try again."}, status=503
        )
    if not rows and error:
        return JsonResponse({"error": error}, status=503)
    data = [
        {
            "player_fpl_id": p.player_fpl_id,
            "player_name": p.player_name,
            "position": p.position,
            "position_type": p.position_type,
            "is_captain": p.is_captain,
            "is_vice_captain": p.is_vice_captain,
            "formatted_cost": p.formatted_cost,
            "form": p.form,
            "expected_goals": p.expected_goals,
            "expected_assists": p.expected_assists,
            "selected_by_percent": p.selected_by_percent,
            "gameweek_points": p.gameweek_points,
            "total_points_at_time": p.total_points_at_time,
            "multiplier": p.multiplier,
        }
        for p in rows
    ]
    return JsonResponse(data, safe=False)


@login_required
@require_http_methods(["GET"])
@never_cache
def team_lineup(request, team_id):
    team = get_object_or_404(Team, id=team_id)
    service = SquadHistoryService(team)
    try:
        selected = service.selection(
            request.GET.get("season"), request.GET.get("gameweek")
        )
    except ValueError as error:
        selected = service.selection()
        return render(
            request,
            "partials/team_lineup.html",
            {"team": team, "squad_selection": selected, "lineup_error": str(error)},
        )
    try:
        rows, error = service.load(selected["season"], selected["gameweek"])
    except Exception:
        import logging

        logging.getLogger(__name__).exception("Lineup unavailable for team %s", team.pk)
        rows, error = [], "We couldn't load this lineup. Please try again."
    total = (
        sum(p.gameweek_points * p.multiplier for p in rows)
        if len(rows) == 15 and all(p.gameweek_points is not None for p in rows)
        else None
    )
    return render(
        request,
        "partials/team_lineup.html",
        {
            "team": team,
            "squad_selection": selected,
            "squad": rows,
            "lineup_error": error,
            "squad_points": total,
            "formation": (
                "-".join(
                    str(
                        sum(
                            p.position <= 11 and p.element_type == position
                            for p in rows
                        )
                    )
                    for position in (2, 3, 4)
                )
                if rows
                else None
            ),
        },
    )


@require_http_methods(["GET", "POST"])
@login_required
@never_cache
def team_analysis(request, team_id):
    team = get_object_or_404(Team, id=team_id)
    if request.user.team_id != team.id:
        return JsonResponse(
            {"error": "Open your own squad to prepare a briefing."}, status=403
        )
    form, free_transfers = planning_input(request, team)
    context = GameweekContextService.cached()
    if request.method == "POST":
        if not form.is_valid():
            return render(
                request,
                "partials/analysis.html",
                {
                    "team": team,
                    "analysis": {
                        "error": "Enter a free-transfer count between 0 and 5, or leave it blank."
                    },
                },
            )
        if not context["season"] or not context["decision_gameweek"]:
            return render(
                request,
                "partials/analysis.html",
                {
                    "team": team,
                    "analysis": {
                        "error": "The next gameweek is currently unavailable."
                    },
                },
            )
        if request.POST.get("season") != context["season"] or request.POST.get(
            "gameweek"
        ) != str(context["decision_gameweek"]):
            return render(
                request,
                "partials/analysis.html",
                {
                    "team": team,
                    "analysis": {
                        "error": "The next gameweek has changed. Reload your squad before preparing a new briefing."
                    },
                },
            )
        free_transfers = form.cleaned_data["free_transfers"]
        TeamPlanningInput.objects.update_or_create(
            user=request.user,
            team=team,
            season=context["season"],
            gameweek=context["decision_gameweek"],
            defaults={"free_transfers": free_transfers},
        )
    result = TeamAnalysisService().analyze_team(
        int(team.fpl_team_id),
        include_team_obj=team,
        free_transfers=free_transfers,
    )
    return render(request, "partials/analysis.html", {"analysis": result, "team": team})


@require_http_methods(["GET"])
@login_required
def team_quick_compare(request, team_id):
    """Quick comparison panel for inline display."""
    rival_team = get_object_or_404(Team, id=team_id)

    if not request.user.team:
        return render(
            request,
            "partials/rival_comparison_quick.html",
            {"error": "You need to have a team to compare."},
        )

    my_team = request.user.team
    if my_team.id == rival_team.id:
        return render(
            request,
            "partials/rival_comparison_quick.html",
            {"error": "Cannot compare your team with itself."},
        )

    analysis_service = TeamAnalysisService()
    result = analysis_service.quick_compare(
        int(my_team.fpl_team_id),
        int(rival_team.fpl_team_id),
        my_team_model=my_team,
        rival_team_model=rival_team,
    )

    return render(
        request,
        "partials/rival_comparison_quick.html",
        {
            "comparison": result,
            "my_team": my_team,
            "rival_team": rival_team,
        },
    )


@login_required
def team_compare(request, team_id):
    """Full dedicated comparison page."""
    rival_team = get_object_or_404(Team, id=team_id)

    if not request.user.team:
        messages.error(request, "You need to have a team to compare.")
        return redirect("rivals:teams.show", team_id=team_id)

    my_team = request.user.team
    if my_team.id == rival_team.id:
        messages.error(request, "Cannot compare your team with itself.")
        return redirect("rivals:teams.show", team_id=team_id)

    analysis_service = TeamAnalysisService()
    result = analysis_service.compare_teams(
        int(my_team.fpl_team_id),
        int(rival_team.fpl_team_id),
        include_models=(my_team, rival_team),
    )

    if result.get("error"):
        messages.error(request, f"Comparison failed: {result['error']}")
        return redirect("rivals:teams.show", team_id=team_id)

    return render(
        request,
        "pages/teams/compare.html",
        {
            "comparison": result,
            "my_team": my_team,
            "rival_team": rival_team,
        },
    )
