from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from rivals.models import Player, Team
from rivals.services.team_analysis_service import TeamAnalysisService
from rivals.services.team_sync_service import TeamSyncService


@login_required
def team_show(request, team_id):
    team = get_object_or_404(Team, id=team_id)

    gameweek_data = team.gameweek_data.order_by("-gameweek")[:10]
    transfers = team.transfers.select_related("player_in", "player_out").order_by(
        "-gameweek"
    )[:20]

    mini_league_performance = calculate_mini_league_performance(team)
    recent_news = get_recent_player_news(transfers)

    return render(
        request,
        "pages/teams/show.html",
        {
            "team": team,
            "gameweek_data": gameweek_data,
            "transfers": transfers,
            "mini_league_performance": mini_league_performance,
            "recent_news": recent_news,
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

    TeamSyncService(team).sync_full_data()
    messages.success(request, "Team is now being tracked!")

    return redirect("rivals:teams.show", team_id=team.id)


@login_required
def team_untrack(request, team_id):
    team = get_object_or_404(Team, id=team_id)
    request.user.tracked_teams.filter(team=team).delete()
    messages.success(request, "Team untracked successfully!")

    return redirect("rivals:teams.show", team_id=team.id)


@login_required
def team_sync(request, team_id):
    team = get_object_or_404(Team, id=team_id)

    success = TeamSyncService(team).sync_full_data()
    if success:
        messages.success(request, "Team synced successfully!")
    else:
        messages.error(request, "Failed to sync team data. Please try again.")

    return redirect("rivals:teams.show", team_id=team.id)


def calculate_mini_league_performance(team):
    results = []
    for mlt in team.mini_league_teams.select_related("mini_league").all():
        total_teams = mlt.mini_league.mini_league_teams.count()
        better_ranks = mlt.mini_league.mini_league_teams.filter(
            current_rank__lte=mlt.current_rank
        ).count()

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

    return sorted(results, key=lambda r: r["current_rank"])


def get_recent_player_news(transfers):
    if not transfers.exists():
        return []

    recent_player_ids = set()
    for t in transfers[:5]:
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


@login_required
def get_squad_history(request, team_id, gameweek):
    print(team_id, gameweek)
    team = get_object_or_404(Team, id=team_id)
    squad = team.squad_histories.filter(gameweek=gameweek).order_by("position")

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
            "total_points_at_time": p.total_points_at_time,
            "multiplier": p.multiplier,
        }
        for p in squad
    ]

    return JsonResponse(data, safe=False)


analysis_service = TeamAnalysisService()


@require_http_methods(["GET"])
@login_required
def team_analysis(request, team_id):
    team = get_object_or_404(Team, id=team_id)
    result = analysis_service.analyze_team(int(team.fpl_team_id), include_team_obj=team)

    return render(request, "partials/analysis.html", {"analysis": result, "team": team})


# @login_required
# def compare_teams(request, team_id, rival_id):
#     pass
