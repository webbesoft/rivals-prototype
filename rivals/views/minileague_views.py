from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from rivals.models import MiniLeague
from rivals.services.fpl_api_service import FplApiService
from rivals.services.mini_league_sync_service import MiniLeagueSyncService


@login_required
def mini_league_index(request):
    mini_leagues = request.user.mini_leagues.prefetch_related("teams").all()
    return render(request, "mini_leagues/index.html", {"mini_leagues": mini_leagues})


@login_required
def mini_league_show(request, mini_league_id):
    mini_league = get_object_or_404(MiniLeague, id=mini_league_id)

    if not request.user.customminileagues.filter(mini_league=mini_league).exists():
        return render(request, "404.html", status=404)

    standings = mini_league.mini_league_teams.select_related("team").order_by(
        "current_rank"
    )

    user_standing = None
    if request.user.team:
        user_standing = standings.filter(team=request.user.team).first()

    return render(
        request,
        "pages/mini_leagues/show.html",
        {
            "mini_league": mini_league,
            "standings": standings,
            "user_standing": user_standing,
        },
    )


@login_required
def mini_league_create(request):
    return render(request, "mini_leagues/create.html")


@login_required
def mini_league_store(request):
    if not request.user.can_add_mini_league():
        messages.error(
            request, "Free plan allows only 1 mini-league. Upgrade to premium."
        )
        return redirect("pricing")

    fpl_league_id = request.POST.get("fpl_league_id", "").strip()
    if not fpl_league_id:
        messages.error(request, "Mini-league ID is required.")
        return redirect("rivals:minileagues.create")

    mini_league, created = MiniLeague.objects.get_or_create(fpl_league_id=fpl_league_id)

    if created:
        fpl_api = FplApiService()
        league_data = fpl_api.fetch_mini_league(fpl_league_id)

        if not league_data:
            messages.error(
                request, "Invalid mini-league code. Please check and try again."
            )
            return redirect("rivals:minileagues.create")

        mini_league.name = league_data["league_info"]["name"]
        mini_league.save()

        MiniLeagueSyncService(mini_league).sync()

    request.user.customminileagues.get_or_create(mini_league=mini_league)
    messages.success(request, "Mini-league added successfully!")

    return redirect("rivals:minileagues.show", mini_league_id=mini_league.id)


@login_required
def mini_league_sync(request, mini_league_id):
    mini_league = get_object_or_404(MiniLeague, id=mini_league_id)

    if not request.user.mini_leagues.filter(id=mini_league.id).exists():
        return render(request, "404.html", status=404)

    success = MiniLeagueSyncService(mini_league).sync()
    if success:
        messages.success(request, "League synced successfully!")
    else:
        messages.error(request, "Failed to sync league. Please try again.")

    return redirect("rivals:minileagues.show", mini_league_id=mini_league.id)
