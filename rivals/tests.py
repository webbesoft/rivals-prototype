from unittest.mock import patch
from django.test import TestCase

from rivals.models import GameweekData, SeasonHistory, Team
from rivals.services.team_sync_service import TeamSyncService


class SeasonTransitionTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(
            fpl_team_id="12345",
            team_name="FC Test",
            manager_name="Test Manager",
            current_total_points=500,
            current_overall_rank=10000,
            current_season="2024/25",
        )
        # Create gameweek data for old season
        GameweekData.objects.create(
            team=self.team,
            gameweek=38,
            season="2024/25",
            points=65,
            total_points=500,
        )

    @patch("rivals.services.fpl_api_service.FplApiService.fetch_current_season")
    def test_same_season_no_transition(self, mock_season):
        mock_season.return_value = "2024/25"
        service = TeamSyncService(self.team)
        transitioned = service.detect_and_handle_season_transition()

        self.assertFalse(transitioned)
        self.assertEqual(self.team.current_season, "2024/25")
        self.assertEqual(self.team.current_total_points, 500)

    @patch("rivals.services.fpl_api_service.FplApiService.fetch_current_season")
    def test_new_season_transition_resets_points_and_preserves_old_data(
        self, mock_season
    ):
        mock_season.return_value = "2025/26"
        service = TeamSyncService(self.team)
        transitioned = service.detect_and_handle_season_transition()

        self.assertTrue(transitioned)
        self.team.refresh_from_db()
        self.assertEqual(self.team.current_season, "2025/26")
        self.assertEqual(self.team.current_total_points, 0)
        self.assertIsNone(self.team.current_overall_rank)

        # Verify old gameweek data is preserved with season label
        old_gw = GameweekData.objects.get(team=self.team, gameweek=38)
        self.assertEqual(old_gw.season, "2024/25")
        self.assertEqual(old_gw.points, 65)

    def test_season_history_creation(self):
        history = SeasonHistory.objects.create(
            team=self.team,
            season_name="2023/24",
            total_points=2150,
            overall_rank=45000,
        )
        self.assertEqual(str(history), "FC Test — 2023/24")
        self.assertEqual(self.team.season_histories.count(), 1)


class DataEngineeringAuditTests(TestCase):
    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.team = Team.objects.create(
            fpl_team_id="99999",
            team_name="Audit FC",
            manager_name="Audit Manager",
            current_season="2025/26",
        )

    @patch("rivals.services.fpl_api_service.requests.get")
    def test_fpl_api_service_caches_bootstrap_static(self, mock_get):
        from rivals.services.fpl_api_service import FplApiService

        mock_get.return_value.json.return_value = {
            "events": [
                {"id": 1, "is_current": True, "deadline_time": "2025-08-15T17:30:00Z"}
            ],
            "elements": [{"id": 10, "web_name": "Haaland", "team": 11}],
            "teams": [{"id": 11, "name": "Man City", "short_name": "MCI"}],
        }
        mock_get.return_value.status_code = 200

        api = FplApiService()
        data1 = api.fetch_bootstrap_static()
        data2 = api.fetch_bootstrap_static()

        self.assertEqual(mock_get.call_count, 1)
        self.assertEqual(data1, data2)
        self.assertEqual(api.fetch_current_gameweek(), 1)
        self.assertEqual(api.fetch_current_season(), "2025/26")
        self.assertEqual(len(api.fetch_elements()), 1)

    def test_squad_history_unique_constraint(self):
        from django.db import IntegrityError, transaction
        from rivals.models import SquadHistory

        SquadHistory.objects.create(
            team=self.team,
            gameweek=1,
            season="2025/26",
            player_fpl_id=101,
            position=1,
            multiplier=1,
            player_cost=50,
            player_name="Player A",
            element_type=1,
        )

        # Same team/gameweek/player/season → must raise
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SquadHistory.objects.create(
                    team=self.team,
                    gameweek=1,
                    season="2025/26",
                    player_fpl_id=101,
                    position=1,
                    multiplier=1,
                    player_cost=50,
                    player_name="Duplicate Player A",
                    element_type=1,
                )

        # Same team/gameweek/player but different season → must succeed
        SquadHistory.objects.create(
            team=self.team,
            gameweek=1,
            season="2024/25",
            player_fpl_id=101,
            position=1,
            multiplier=1,
            player_cost=48,
            player_name="Player A",
            element_type=1,
        )

    @patch("rivals.services.fpl_api_service.FplApiService.fetch_fixtures_data")
    @patch("rivals.services.fpl_api_service.FplApiService.fetch_teams")
    def test_landing_data_service_dynamic_fixtures(self, mock_teams, mock_fixtures):
        from rivals.services.landing_data_service import LandingDataService

        mock_teams.return_value = [
            {"id": 1, "name": "Arsenal", "short_name": "ARS"},
            {"id": 2, "name": "Chelsea", "short_name": "CHE"},
        ]
        mock_fixtures.return_value = [
            {
                "event": 2,
                "finished": False,
                "team_h": 1,
                "team_a": 2,
                "team_h_difficulty": 2,
                "team_a_difficulty": 4,
            }
        ]

        landing = LandingDataService()
        highlights = landing.get_fixture_difficulty_highlights()

        self.assertEqual(len(highlights["best_fixtures"]), 2)
        self.assertEqual(highlights["best_fixtures"][0]["team"], "Arsenal")
        self.assertEqual(highlights["best_fixtures"][0]["difficulty"], 2)
        self.assertEqual(highlights["worst_fixtures"][0]["team"], "Chelsea")
        self.assertEqual(highlights["worst_fixtures"][0]["difficulty"], 4)

    @patch("rivals.services.fpl_api_service.FplApiService.fetch_mini_league")
    @patch("rivals.services.fpl_api_service.FplApiService.fetch_current_season")
    def test_mini_league_sync_sets_season(self, mock_season, mock_league):
        from rivals.models import MiniLeague, MiniLeagueTeam
        from rivals.services.mini_league_sync_service import MiniLeagueSyncService

        mock_season.return_value = "2025/26"
        mock_league.return_value = {
            "league_info": {"name": "Test League"},
            "standings": [
                {
                    "entry": 99999,
                    "entry_name": "Audit FC",
                    "player_name": "Audit Mgr",
                    "rank": 1,
                    "total": 120,
                }
            ],
        }

        league = MiniLeague.objects.create(fpl_league_id="111", name="Old Name")
        sync_service = MiniLeagueSyncService(league)
        success = sync_service.sync()

        self.assertTrue(success)
        mlt = MiniLeagueTeam.objects.get(mini_league=league, team=self.team)
        self.assertEqual(mlt.season, "2025/26")
        self.assertEqual(mlt.current_rank, 1)


class ProjectionServiceTests(TestCase):
    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.team = Team.objects.create(
            fpl_team_id="88888",
            team_name="Projection FC",
            manager_name="Projection Manager",
            current_total_points=600,
            current_overall_rank=75000,
            current_season="2025/26",
        )
        # Create 10 completed gameweeks (60 pts each = 600 total)
        for gw in range(1, 11):
            GameweekData.objects.create(
                team=self.team,
                gameweek=gw,
                season="2025/26",
                points=60,
                total_points=gw * 60,
                rank=75000,
            )

    def test_points_to_overall_rank(self):
        from rivals.services.projection_service import ProjectionService

        # High points benchmark
        rank_top = ProjectionService.points_to_overall_rank(2500)
        self.assertEqual(rank_top, 10000)

        # Mid points
        rank_mid = ProjectionService.points_to_overall_rank(2080)
        self.assertEqual(rank_mid, 1000000)

        # Monotonicity check
        rank_higher = ProjectionService.points_to_overall_rank(2400)
        rank_lower = ProjectionService.points_to_overall_rank(2200)
        self.assertLess(rank_higher, rank_lower)

    def test_project_team_finish(self):
        from rivals.services.projection_service import ProjectionService

        service = ProjectionService()
        proj = service.project_team_finish(self.team)

        # 10 completed GWs, 28 remaining. Pace is 60 PPG.
        # Projected points = 600 + (28 * 60) = 2280
        self.assertEqual(proj["completed_gameweeks"], 10)
        self.assertEqual(proj["remaining_gameweeks"], 28)
        self.assertEqual(proj["current_points"], 600)
        self.assertEqual(proj["season_ppg"], 60.0)
        self.assertEqual(proj["projected_points"], 2280)
        self.assertGreater(proj["projected_points_max"], proj["projected_points"])
        self.assertLess(proj["projected_points_min"], proj["projected_points"])
        self.assertIsNotNone(proj["projected_overall_rank"])
        self.assertIn("Top", proj["projected_rank_tier"])

    def test_project_mini_league(self):
        from rivals.models import MiniLeague, MiniLeagueTeam
        from rivals.services.projection_service import ProjectionService

        league = MiniLeague.objects.create(
            fpl_league_id="999", name="Premier Rival League"
        )

        team_b = Team.objects.create(
            fpl_team_id="77777",
            team_name="Rival FC",
            manager_name="Rival Manager",
            current_total_points=650,
            current_overall_rank=40000,
            current_season="2025/26",
        )

        MiniLeagueTeam.objects.create(
            mini_league=league,
            team=self.team,
            current_rank=2,
            total_points=600,
        )
        MiniLeagueTeam.objects.create(
            mini_league=league,
            team=team_b,
            current_rank=1,
            total_points=650,
        )

        service = ProjectionService()
        result = service.project_mini_league(league, current_gw=11)

        self.assertEqual(result["total_teams"], 2)
        standings = result["standings"]
        self.assertEqual(standings[0]["team_id"], team_b.id)
        self.assertEqual(standings[0]["projected_rank"], 1)
        self.assertEqual(standings[1]["team_id"], self.team.id)
        self.assertEqual(standings[1]["projected_rank"], 2)

        # Single team summary lookup
        summary = service.get_team_mini_league_summary(self.team, league)
        self.assertIsNotNone(summary)
        self.assertEqual(summary["projected_rank"], 2)
        self.assertGreater(summary["gap_to_leader"], 0)


class DashboardViewTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        from rivals.models import (
            MiniLeague,
            MiniLeagueTeam,
            TrackedTeam,
            Transfer,
            UserMiniLeague,
        )

        User = get_user_model()
        self.user = User.objects.create_user(
            username="dashmgr@example.com",
            email="dashmgr@example.com",
            password="password123",
            first_name="Dash",
            last_name="Manager",
            is_verified=True,
        )
        self.team = Team.objects.create(
            fpl_team_id="10101",
            team_name="Command FC",
            manager_name="Dash Manager",
            current_total_points=850,
            current_overall_rank=25000,
            current_season="2025/26",
        )
        self.user.team = self.team
        self.user.save()

        # Mini league
        self.league = MiniLeague.objects.create(fpl_league_id="500", name="Apex League")
        UserMiniLeague.objects.create(user=self.user, mini_league=self.league)
        MiniLeagueTeam.objects.create(
            mini_league=self.league,
            team=self.team,
            current_rank=1,
            total_points=850,
            season="2025/26",
        )

        # Tracked rival
        self.rival_team = Team.objects.create(
            fpl_team_id="20202",
            team_name="Rival XI",
            manager_name="Rival Boss",
            current_total_points=830,
            current_overall_rank=31000,
            current_season="2025/26",
        )
        TrackedTeam.objects.create(user=self.user, team=self.rival_team)

        # Transfer activity by rival
        Transfer.objects.create(
            team=self.rival_team,
            gameweek=15,
            season="2025/26",
            player_in_name="Cole Palmer",
            player_out_name="Bukayo Saka",
        )

    def test_dashboard_renders_successfully(self):
        from django.urls import reverse

        self.client.login(username="dashmgr@example.com", password="password123")
        response = self.client.get(reverse("rivals:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Command FC")
        self.assertContains(response, "Apex League")
        self.assertContains(response, "Rival XI")
        self.assertContains(response, "Cole Palmer")
        self.assertContains(response, "Bukayo Saka")

    def test_team_show_renders_successfully(self):
        from django.urls import reverse

        self.client.login(username="dashmgr@example.com", password="password123")
        response = self.client.get(
            reverse("rivals:teams.show", kwargs={"team_id": self.team.id})
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Command FC")
        self.assertContains(response, "FPL Squad Dossier")
        self.assertContains(response, "Mini-League Standings")
        self.assertContains(response, "Apex League")

    def test_mini_league_show_renders_successfully(self):
        from django.urls import reverse

        self.client.login(username="dashmgr@example.com", password="password123")
        response = self.client.get(
            reverse(
                "rivals:minileagues.show", kwargs={"mini_league_id": self.league.id}
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Apex League")
        self.assertContains(response, "Command FC")
        self.assertContains(response, "League Standings & Projections")
