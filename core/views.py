from django.shortcuts import render
from django.http import HttpResponse

from rivals.services.landing_data_service import LandingDataService


# Create your views here.
def index(request):
    landing_service = LandingDataService()

    context = {
        "current_gameweek": landing_service.get_current_gameweek(),
        "captain_picks": landing_service.get_top_captain_picks(),
        "transfer_trends": landing_service.get_transfer_trends(),
        "fixture_highlights": landing_service.get_fixture_difficulty_highlights(),
        "demo_team": landing_service.get_demo_team_data(),
    }

    return render(request, "pages/index.html", context)


def health_check(request):
    return HttpResponse("OK")
