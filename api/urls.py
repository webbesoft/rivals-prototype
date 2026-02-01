from django.urls import path

from . import views

app_name = "api"

urlpatterns = [
    # Auth
    path("auth/login/", views.LoginView.as_view(), name="login"),
    path("me/", views.MeView.as_view(), name="me"),
    # Dashboard
    path("dashboard/", views.DashboardView.as_view(), name="dashboard"),
    # Leagues
    path("leagues/", views.LeaguesView.as_view(), name="leagues"),
    path("leagues/<int:league_id>/", views.LeagueDetailView.as_view(), name="league_detail"),
    # Teams
    path("teams/tracked/", views.TrackedTeamsView.as_view(), name="tracked_teams"),
    path("teams/<int:team_id>/", views.TeamDetailView.as_view(), name="team_detail"),
    path("teams/<int:team_id>/gameweeks/", views.TeamGameweekHistoryView.as_view(), name="team_gameweeks"),
    path("teams/<int:team_id>/transfers/", views.TeamTransfersView.as_view(), name="team_transfers"),
    path("teams/<int:team_id>/squad/", views.TeamSquadView.as_view(), name="team_squad"),
    path("teams/<int:team_id>/sync/", views.TeamSyncView.as_view(), name="team_sync"),
    path("teams/<int:team_id>/track/", views.TeamTrackView.as_view(), name="team_track"),
]
