from django.contrib import admin
from django.urls import path

from . import views
from .views import (
    ai_views,
    dashboard_views,
    decision_views,
    minileague_views,
    team_views,
)

app_name = "rivals"

urlpatterns = [
    path("decisions/ai/", ai_views.briefing, name="decisions.ai"),
    path("", dashboard_views.dashboard, name="dashboard"),
    path("decisions/", decision_views.planner, name="decisions"),
    path("decisions/summary/", decision_views.summary, name="decisions.summary"),
    path("decisions/workspace/", decision_views.workspace, name="decisions.workspace"),
    path("decisions/save/", decision_views.save_decision, name="decisions.save"),
    path("decisions/journal/", decision_views.journal, name="decisions.journal"),
    path(
        "decisions/journal/<int:decision_id>/",
        decision_views.journal_status,
        name="decisions.status",
    ),
    path(
        "refresh-status/",
        dashboard_views.dashboard_refresh_status,
        name="refresh-status",
    ),
    path("intel/", dashboard_views.dashboard_intel, name="intel"),
    path("rivals/sync/", dashboard_views.dashboard_sync_rivals, name="rivals.sync"),
    path("teams/", team_views.team_show, name="teams.index"),
    path("teams/<int:team_id>/", team_views.team_show, name="teams.show"),
    path(
        "teams/<int:team_id>/performance/",
        team_views.team_show,
        {"section": "performance"},
        name="teams.performance",
    ),
    path(
        "teams/<int:team_id>/transfers/",
        team_views.team_show,
        {"section": "transfers"},
        name="teams.transfers",
    ),
    path(
        "teams/<int:team_id>/history/",
        team_views.team_show,
        {"section": "history"},
        name="teams.history",
    ),
    path("teams/<int:team_id>/score/", team_views.team_score, name="teams.score"),
    path("teams/<int:team_id>/track/", team_views.team_track, name="teams.track"),
    path("teams/<int:team_id>/untrack/", team_views.team_untrack, name="teams.untrack"),
    path("teams/<int:team_id>/sync/", team_views.team_sync, name="teams.sync"),
    path(
        "teams/<int:team_id>/analysis/", team_views.team_analysis, name="teams.analysis"
    ),
    path(
        "teams/<int:team_id>/compare/quick/",
        team_views.team_quick_compare,
        name="teams.compare.quick",
    ),
    path(
        "teams/<int:team_id>/compare/",
        team_views.team_compare,
        name="teams.compare",
    ),
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
    path("teams/<int:team_id>/lineup/", team_views.team_lineup, name="teams.lineup"),
    # Squad History
    path(
        "teams/<int:team_id>/squad-history/<int:gameweek>/",
        team_views.get_squad_history,
        name="teams.squad-history",
    ),
]
