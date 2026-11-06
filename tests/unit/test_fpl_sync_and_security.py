from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory
from django.urls import reverse

from core.middleware import BlockScannerMiddleware
from rivals.models import MiniLeague, MiniLeagueTeam, Team, UserMiniLeague
from rivals.services.init_user_service import InitUserService
from rivals.services.mini_league_sync_service import MiniLeagueSyncService

User = get_user_model()


@pytest.fixture
def auth_user():
    return User.objects.create_user(
        email="manager@example.com",
        username="manager@example.com",
        password="secretpassword123",
        fpl_id="11111",
        team_name="Old FC",
        manager_name="Old Manager",
        is_verified=True,
    )


@pytest.mark.django_db
class TestFplIdUpdateAndSync:
    @patch("rivals.services.team_sync_service.TeamSyncService.sync_full_data")
    @patch("rivals.services.mini_league_sync_service.MiniLeagueSyncService.sync")
    @patch("rivals.services.fpl_api_service.FplApiService.fetch_team_basic_info")
    def test_update_fpl_id_success(
        self, mock_fetch_info, mock_ml_sync, mock_team_sync, auth_user
    ):
        old_team = Team.objects.create(
            fpl_team_id="11111",
            team_name="Old FC",
            manager_name="Old Manager",
            current_total_points=50,
            current_overall_rank=500000,
        )
        auth_user.team = old_team
        auth_user.save()

        old_league = MiniLeague.objects.create(
            fpl_league_id="9901",
            name="Friends League Old",
        )
        UserMiniLeague.objects.create(user=auth_user, mini_league=old_league)

        mock_fetch_info.return_value = {
            "id": 22222,
            "name": "New Campaign FC",
            "player_first_name": "New",
            "player_last_name": "Boss",
            "summary_overall_points": 210,
            "summary_overall_rank": 15000,
            "leagues": {
                "classic": [
                    {
                        "id": 9902,
                        "name": "Friends League New",
                        "short_name": "fl",
                        "league_type": "x",
                        "scoring": "c",
                        "rank_count": 12,
                    }
                ]
            },
        }

        client = Client()
        client.force_login(auth_user)

        response = client.post(
            reverse("accounts:update_fpl_id"),
            {"fpl_id": 22222},
        )

        assert response.status_code == 302
        assert response.url == reverse("rivals:dashboard")

        auth_user.refresh_from_db()
        assert auth_user.team is not None
        assert auth_user.team.fpl_team_id == "22222"
        assert auth_user.team.team_name == "New Campaign FC"
        assert auth_user.fpl_id == "22222"
        assert auth_user.team_name == "New Campaign FC"

        # Stale league pruned, new league present
        user_leagues = list(
            auth_user.customminileagues.values_list(
                "mini_league__fpl_league_id", flat=True
            )
        )
        assert "9902" in user_leagues
        assert "9901" not in user_leagues

    @patch("accounts.tasks.init_user_data_task.now")
    @patch("rivals.services.fpl_api_service.FplApiService.fetch_team_basic_info")
    def test_update_fpl_id_failure_restores_prior_state(
        self, mock_fetch_info, mock_task_now, auth_user
    ):
        old_team = Team.objects.create(
            fpl_team_id="11111",
            team_name="Old FC",
            manager_name="Old Manager",
        )
        auth_user.team = old_team
        auth_user.save()

        mock_fetch_info.return_value = {
            "id": 99999,
            "name": "Failed Team",
            "player_first_name": "Fail",
            "player_last_name": "User",
            "summary_overall_points": 0,
            "summary_overall_rank": 0,
            "leagues": {"classic": []},
        }

        # Simulate task error
        mock_task_now.side_effect = RuntimeError("Database connection lost")

        client = Client()
        client.force_login(auth_user)

        response = client.post(
            reverse("accounts:update_fpl_id"),
            {"fpl_id": 99999},
        )

        assert response.status_code == 302
        assert response.url == reverse("accounts:profile")

        auth_user.refresh_from_db()
        assert auth_user.fpl_id == "11111"
        assert auth_user.team_name == "Old FC"
        assert auth_user.manager_name == "Old Manager"
        assert auth_user.team == old_team


@pytest.mark.django_db
class TestInitUserServiceRobustness:
    @patch("rivals.services.team_sync_service.TeamSyncService.sync_full_data")
    def test_init_service_succeeds_even_if_sync_full_data_fails(
        self, mock_team_sync, auth_user
    ):
        mock_team_sync.side_effect = Exception("FPL API timeout")

        basic_info = {
            "id": 33333,
            "name": "Resilient FC",
            "player_first_name": "Resilient",
            "player_last_name": "Boss",
            "summary_overall_points": 100,
            "summary_overall_rank": 50000,
            "leagues": {"classic": []},
        }

        service = InitUserService(auth_user)
        result = service.save_user_info(basic_info)

        assert result is True
        auth_user.refresh_from_db()
        assert auth_user.team is not None
        assert auth_user.team.fpl_team_id == "33333"


@pytest.mark.django_db
class TestDashboardViews:
    @patch("rivals.services.team_sync_service.TeamSyncService.sync_full_data")
    @patch("rivals.services.projection_service.ProjectionService.project_team_finish")
    @patch(
        "rivals.services.projection_service.ProjectionService.get_team_mini_league_summary"
    )
    def test_dashboard_self_heals_unlinked_team(
        self, mock_ml_proj, mock_team_proj, mock_team_sync, auth_user
    ):
        team = Team.objects.create(
            fpl_team_id="11111",
            team_name="Restored FC",
            manager_name="Restored Manager",
            current_season="2025/26",
            current_total_points=120,
            current_overall_rank=45000,
        )
        auth_user.team = None
        auth_user.fpl_id = "11111"
        auth_user.save()

        mock_team_proj.return_value = {
            "completed_gameweeks": 5,
            "remaining_gameweeks": 33,
            "projected_points": 2100,
            "projected_overall_rank": 35000,
            "rank_change": 10000,
            "season_ppg": 24.0,
            "recent_ppg": 25.0,
            "projected_ppg": 24.5,
            "projected_points_min": 1900,
            "projected_points_max": 2300,
            "projected_rank_min": 20000,
            "projected_rank_max": 50000,
            "projected_rank_tier": "Top 50k",
            "rank_trajectory": "improving",
        }
        mock_ml_proj.return_value = None

        client = Client()
        client.force_login(auth_user)

        response = client.get(reverse("rivals:dashboard"))
        assert response.status_code == 200

        auth_user.refresh_from_db()
        assert auth_user.team == team
        assert "Restored FC" in response.content.decode()

    def test_dashboard_does_not_mutate_or_delete_associations_on_get(self, auth_user):
        team = Team.objects.create(
            fpl_team_id="11111",
            team_name="Restored FC",
            current_season="2025/26",
            current_total_points=120,
            current_overall_rank=45000,
        )
        auth_user.team = team
        auth_user.save()

        ml1 = MiniLeague.objects.create(fpl_league_id="801", name="League 1")
        ml2 = MiniLeague.objects.create(fpl_league_id="802", name="League 2")

        UserMiniLeague.objects.create(user=auth_user, mini_league=ml1)
        UserMiniLeague.objects.create(user=auth_user, mini_league=ml2)

        client = Client()
        client.force_login(auth_user)

        response = client.get(reverse("rivals:dashboard"))
        assert response.status_code == 200

        # Associations are preserved without side-effect deletion on GET
        assert auth_user.customminileagues.count() == 2


@pytest.mark.django_db
class TestMiniLeagueSyncPruning:
    @patch("rivals.services.fpl_api_service.FplApiService.fetch_current_season")
    @patch("rivals.services.fpl_api_service.FplApiService.fetch_mini_league")
    def test_sync_prunes_stale_teams_when_complete(
        self, mock_fetch_ml, mock_current_season
    ):
        mock_current_season.return_value = "2025/26"

        league = MiniLeague.objects.create(fpl_league_id="555", name="Testing League")
        team_old = Team.objects.create(fpl_team_id="101", team_name="Old Team")
        team_current = Team.objects.create(fpl_team_id="102", team_name="Current Team")

        MiniLeagueTeam.objects.create(
            mini_league=league,
            team=team_old,
            current_rank=10,
            total_points=50,
            season="2024/25",
        )
        MiniLeagueTeam.objects.create(
            mini_league=league,
            team=team_current,
            current_rank=1,
            total_points=100,
            season="2025/26",
        )

        mock_fetch_ml.return_value = {
            "league_info": {"name": "Testing League"},
            "standings": [
                {
                    "entry": 102,
                    "entry_name": "Current Team",
                    "player_name": "Player Two",
                    "rank": 1,
                    "total": 105,
                }
            ],
            "is_complete": True,
        }

        sync_service = MiniLeagueSyncService(league)
        success = sync_service.sync()

        assert success is True
        assert league.mini_league_teams.count() == 1
        assert league.mini_league_teams.first().team == team_current

    @patch("rivals.services.fpl_api_service.FplApiService.fetch_current_season")
    @patch("rivals.services.fpl_api_service.FplApiService.fetch_mini_league")
    def test_sync_does_not_prune_when_incomplete(
        self, mock_fetch_ml, mock_current_season
    ):
        mock_current_season.return_value = "2025/26"

        league = MiniLeague.objects.create(fpl_league_id="556", name="Partial League")
        team_old = Team.objects.create(fpl_team_id="201", team_name="Team 1")
        team_current = Team.objects.create(fpl_team_id="202", team_name="Team 2")

        MiniLeagueTeam.objects.create(
            mini_league=league, team=team_old, current_rank=1, total_points=50
        )
        MiniLeagueTeam.objects.create(
            mini_league=league, team=team_current, current_rank=2, total_points=40
        )

        # Standings is marked as incomplete (partial page)
        mock_fetch_ml.return_value = {
            "league_info": {"name": "Partial League"},
            "standings": [
                {
                    "entry": 202,
                    "entry_name": "Team 2",
                    "player_name": "Player Two",
                    "rank": 2,
                    "total": 45,
                }
            ],
            "is_complete": False,
        }

        sync_service = MiniLeagueSyncService(league)
        success = sync_service.sync()

        assert success is True
        # Both teams should remain because response was incomplete
        assert league.mini_league_teams.count() == 2

    @patch("rivals.services.fpl_api_service.FplApiService.fetch_current_season")
    @patch("rivals.services.fpl_api_service.FplApiService.fetch_mini_league")
    def test_sync_does_not_prune_on_resolution_failure(
        self, mock_fetch_ml, mock_current_season
    ):
        mock_current_season.return_value = "2025/26"

        league = MiniLeague.objects.create(
            fpl_league_id="557", name="Resolution Fail League"
        )
        team1 = Team.objects.create(fpl_team_id="301", team_name="Team 1")
        team2 = Team.objects.create(fpl_team_id="302", team_name="Team 2")

        MiniLeagueTeam.objects.create(
            mini_league=league, team=team1, current_rank=1, total_points=50
        )
        MiniLeagueTeam.objects.create(
            mini_league=league, team=team2, current_rank=2, total_points=40
        )

        mock_fetch_ml.return_value = {
            "league_info": {"name": "Resolution Fail League"},
            "standings": [
                {
                    "entry": 301,
                    "entry_name": "Team 1",
                    "player_name": "Player One",
                    "rank": 1,
                    "total": 55,
                },
                {
                    # Corrupt entry that causes _find_or_create_team to return None
                    "entry": None,
                    "entry_name": "Bad Entry",
                    "player_name": "Bad",
                    "rank": 2,
                    "total": 0,
                },
            ],
            "is_complete": True,
        }

        sync_service = MiniLeagueSyncService(league)
        success = sync_service.sync()

        assert success is True
        # Pruning aborted because resolution failure occurred
        assert league.mini_league_teams.count() == 2


class TestBlockScannerMiddleware:
    def test_blocks_sensitive_probes(self):
        middleware = BlockScannerMiddleware(MagicMock())
        rf = RequestFactory()

        sensitive_paths = [
            "/.env",
            "/.env.development",
            "/.env.local",
            "/test/.env",
            "/.aws/credentials",
            "/.git/config",
            "/config.php.save",
            "/info.php",
            "/wp-admin/install.php",
            "/phpmyadmin",
        ]

        for path in sensitive_paths:
            request = rf.get(path)
            response = middleware(request)
            assert response.status_code == 404
            assert response.content == b"Not Found"

    def test_allows_normal_requests(self):
        get_response_mock = MagicMock(return_value="OK")
        middleware = BlockScannerMiddleware(get_response_mock)
        rf = RequestFactory()

        normal_paths = [
            "/",
            "/accounts/login/",
            "/accounts/profile/",
            "/dashboard/",
            "/static/css/output.css",
        ]

        for path in normal_paths:
            request = rf.get(path)
            response = middleware(request)
            assert response == "OK"
