from django.urls import path

from . import views, planning_views

app_name = "api"

urlpatterns = [
    # Auth
    path("auth/login/", views.LoginView.as_view(), name="login"),
    path("auth/signup/", views.SignUpView.as_view(), name="signup"),
    path("me/", views.MeView.as_view(), name="me"),
    # Dashboard
    path("dashboard/", views.DashboardView.as_view(), name="dashboard"),
    # Leagues
    path("leagues/", views.LeaguesView.as_view(), name="leagues"),
    path("leagues/<int:league_id>/", views.LeagueDetailView.as_view(), name="league_detail"),
    path("leagues/<int:league_id>/sync/", views.LeagueSyncView.as_view(), name="league_sync"),
    # Teams
    path("teams/tracked/", views.TrackedTeamsView.as_view(), name="tracked_teams"),
    path("teams/<int:team_id>/", views.TeamDetailView.as_view(), name="team_detail"),
    path("teams/<int:team_id>/gameweeks/", views.TeamGameweekHistoryView.as_view(), name="team_gameweeks"),
    path("teams/<int:team_id>/transfers/", views.TeamTransfersView.as_view(), name="team_transfers"),
    path("teams/<int:team_id>/squad/", views.TeamSquadView.as_view(), name="team_squad"),
    path("teams/<int:team_id>/sync/", views.TeamSyncView.as_view(), name="team_sync"),
    path("teams/<int:team_id>/track/", views.TeamTrackView.as_view(), name="team_track"),
    # Planning
    path("planning/plans/", planning_views.TransferPlanListCreateView.as_view(), name="plans_list_create"),
    path("planning/plans/<int:pk>/", planning_views.TransferPlanDetailView.as_view(), name="plan_detail"),
    path("planning/plans/<int:plan_id>/transfers/", planning_views.PlannedTransferCreateView.as_view(), name="plan_transfer_create"),
    path("planning/transfers/<int:pk>/", planning_views.PlannedTransferDeleteView.as_view(), name="plan_transfer_delete"),
    path("planning/projections/<int:team_id>/", planning_views.ProjectionsView.as_view(), name="projections"),
    path("planning/suggestions/", planning_views.SuggestionsView.as_view(), name="suggestions"),
]
