from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.rate_limits import rate_limit
from rivals.models import MiniLeague
from rivals.services.access_service import accessible_leagues
from rivals.services.gameweek_context_service import GameweekContextService
from rivals.services.projection_service import ProjectionService
from rivals.services.refresh_service import enqueue_refresh, refresh_status


@login_required
def mini_league_index(request):
    mini_leagues = accessible_leagues(request.user)
    return render(request, "mini_leagues/index.html", {"mini_leagues": mini_leagues})


@login_required
def mini_league_show(request, mini_league_id):
    mini_league = get_object_or_404(MiniLeague, id=mini_league_id)

    if not accessible_leagues(request.user).filter(pk=mini_league.pk).exists():
        raise Http404

    enqueue_refresh("league", mini_league.id)
    if mini_league.uses_summary:
        from rivals.services.league_summary_service import large_league_context

        return render(
            request,
            "pages/mini_leagues/large.html",
            {
                **refresh_status(request.user),
                **large_league_context(
                    request.user,
                    mini_league,
                    GameweekContextService.cached(),
                    request.GET,
                ),
                "mini_league": mini_league,
            },
        )
    standings = mini_league.mini_league_teams.select_related("team").order_by(
        "current_rank"
    )

    user_standing = None
    if request.user.team:
        user_standing = standings.filter(team=request.user.team).first()

    league_projection = ProjectionService().project_mini_league(mini_league)
    projected_by_team_id = {
        s["team_id"]: s for s in league_projection.get("standings", [])
    }

    user_league_projection = (
        projected_by_team_id.get(request.user.team.id) if request.user.team else None
    )

    tracked_team_ids = set(request.user.tracked_teams.values_list("team_id", flat=True))

    # Attach projected metrics to standings objects for convenient template access
    for standing in standings:
        proj = projected_by_team_id.get(standing.team_id)
        if proj:
            standing.projected_rank = proj["projected_rank"]
            standing.projected_points = proj["projected_points"]
            standing.projected_rank_change = proj["rank_change"]
            standing.projected_pace = proj["pace_ppg"]
        else:
            standing.projected_rank = standing.current_rank
            standing.projected_points = standing.total_points
            standing.projected_rank_change = 0
            standing.projected_pace = 0.0

    return render(
        request,
        "pages/mini_leagues/show.html",
        {
            **refresh_status(request.user),
            "mini_league": mini_league,
            "standings": standings,
            "user_standing": user_standing,
            "league_projection": league_projection,
            "user_league_projection": user_league_projection,
            "tracked_team_ids": tracked_team_ids,
        },
    )


@login_required
def mini_league_create(request):
    return render(request, "mini_leagues/create.html")


@login_required
@require_POST
@rate_limit("league_add")
def mini_league_store(request):
    from django.contrib.auth import get_user_model
    from django.db import transaction

    from rivals.models import UserMiniLeague
    from rivals.services.manual_league_service import validated_league

    try:
        fpl_id, name = validated_league(request.POST.get("fpl_league_id", ""))
    except ValueError as error:
        messages.error(request, str(error))
        return redirect("rivals:minileagues.create")

    try:
        with transaction.atomic():
            user = get_user_model().objects.select_for_update().get(pk=request.user.pk)
            existing = accessible_leagues(user).filter(fpl_league_id=fpl_id).exists()
            if not existing and not user.can_add_mini_league:
                raise ValueError("You can add up to 10 mini-leagues.")
            mini_league, _ = MiniLeague.objects.update_or_create(
                fpl_league_id=fpl_id, defaults={"name": name, "active": True}
            )
            UserMiniLeague.objects.update_or_create(
                user=user,
                mini_league=mini_league,
                defaults={"source": "manual", "active": True},
            )
            enqueue_refresh("league", mini_league.pk)
    except ValueError as error:
        messages.error(request, str(error))
        return redirect("rivals:minileagues.create")
    except Exception:
        import logging

        logging.getLogger(__name__).exception("Manual league import failed")
        messages.error(request, "Could not add the league. Please try again.")
        return redirect("rivals:minileagues.create")

    messages.success(request, "Mini-league added successfully!")
    return redirect("rivals:minileagues.show", mini_league_id=mini_league.pk)


@login_required
@require_POST
@rate_limit("refresh")
def mini_league_sync(request, mini_league_id):
    mini_league = get_object_or_404(MiniLeague, id=mini_league_id)
    if not accessible_leagues(request.user).filter(pk=mini_league.pk).exists():
        raise Http404
    enqueue_refresh("league", mini_league.id, force=True)
    if mini_league.uses_summary and request.user.team_id:
        enqueue_refresh("profile", request.user.team_id, force=True)
    messages.info(request, "Standings update requested.")
    return redirect("rivals:minileagues.show", mini_league_id=mini_league.id)


@login_required
@require_POST
def mini_league_track(request, mini_league_id, page_number, entry_id):
    from django.db import transaction

    from rivals.models import LeagueStandingsPage, Team
    from rivals.services.access_service import TrackingLimitReached, track_team
    from rivals.services.refresh_service import enqueue_team

    league = get_object_or_404(accessible_leagues(request.user), pk=mini_league_id)
    context = GameweekContextService.cached()
    page = get_object_or_404(
        LeagueStandingsPage,
        mini_league=league,
        season=context["season"],
        number=page_number,
    )
    row = next((row for row in page.rows if row["entry"] == entry_id), None)
    if row is None:
        raise Http404
    try:
        with transaction.atomic():
            team, _ = Team.objects.get_or_create(
                fpl_team_id=str(entry_id),
                defaults={
                    "team_name": row["entry_name"],
                    "manager_name": row["player_name"],
                },
            )
            track_team(request.user, team)
    except TrackingLimitReached as error:
        messages.error(request, str(error))
        return redirect("rivals:minileagues.show", mini_league_id=league.pk)
    enqueue_team(team.pk)
    messages.success(request, "Team is now being tracked.")
    return redirect("rivals:teams.show", team_id=team.pk)
