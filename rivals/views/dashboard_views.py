from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import render

from rivals.models import GameweekData, Transfer


@login_required
def dashboard(request):
    mini_leagues_qs = request.user.customminileagues.select_related(
        "mini_league"
    ).order_by("mini_league__name")

    paginator = Paginator(mini_leagues_qs, 10)  # 10 per page
    page_number = request.GET.get("page")
    mini_leagues = paginator.get_page(page_number)

    user_team = request.user.team
    recent_leagues = mini_leagues[:5]
    tracked_teams = request.user.tracked_teams.select_related("team").all()[:5]
    recent_activity = fetch_recent_activity(request.user)

    user_gameweek_data = None
    if request.user.team_id:
        user_gameweek_data = request.user.team.gameweek_data.filter(
            rank__isnull=False
        ).order_by("gameweek")

    return render(
        request,
        "pages/dashboard.html",
        {
            "mini_leagues": mini_leagues,
            "user_team": user_team,
            "recent_leagues": recent_leagues,
            "tracked_teams": tracked_teams,
            "recent_activity": recent_activity,
            "user_gameweek_data": user_gameweek_data,
        },
    )


def fetch_recent_activity(user):
    team_ids = user.tracked_teams.values_list("team_id", flat=True)

    return {
        "transfers": Transfer.objects.filter(team_id__in=team_ids)
        .select_related("team", "player_in", "player_out")
        .order_by("-created_at")[:10],
        "gameweek_data": GameweekData.objects.filter(team_id__in=team_ids)
        .select_related("team")
        .order_by("-created_at")[:10],
    }
