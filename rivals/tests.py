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
    def test_new_season_transition_resets_points_and_preserves_old_data(self, mock_season):
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
