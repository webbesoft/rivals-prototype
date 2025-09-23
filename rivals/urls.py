from django.contrib import admin
from django.urls import path

from . import views
from .views import dashboard_views, minileague_views, team_views

app_name = "rivals"

urlpatterns = [
    path("", dashboard_views.dashboard, name="dashboard"),
    path("teams/", team_views.team_show, name="teams.index"),
    path("teams/<int:team_id>/", team_views.team_show, name="teams.show"),
    path("teams/<int:team_id>/track/", team_views.team_track, name="teams.track"),
    path("teams/<int:team_id>/untrack/", team_views.team_untrack, name="teams.untrack"),
    path("teams/<int:team_id>/sync/", team_views.team_sync, name="teams.sync"),
    path(
        "teams/<int:team_id>/analysis/", team_views.team_analysis, name="teams.analysis"
    ),
    # path("teams/<int:team_id>/compare/<int:rival_id>/", team_views.compare_teams, name="compare"),
    # Mini Leagues
    path("mini-leagues/", minileague_views.mini_league_index, name="minileagues.index"),
    path(
        "mini-leagues/create/",
        minileague_views.mini_league_create,
        name="minileagues.create",
    ),
    path(
        "mini-leagues/store/",
        minileague_views.mini_league_store,
        name="minileagues.store",
    ),  # Django usually merges create/store
    path(
        "mini-leagues/<int:mini_league_id>/",
        minileague_views.mini_league_show,
        name="minileagues.show",
    ),
    path(
        "mini-leagues/<int:mini_league_id>/sync/",
        minileague_views.mini_league_sync,
        name="minileagues.sync",
    ),
    # Squad History
    path(
        "teams/<int:team_id>/squad-history/<int:gameweek>/",
        team_views.get_squad_history,
        name="teams.squad-history",
    ),
]
