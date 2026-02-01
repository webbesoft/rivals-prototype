from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework.authtoken.models import Token
from rivals.models import Team, MiniLeague, MiniLeagueTeam, UserMiniLeague

User = get_user_model()


class ApiViewsTestCase(APITestCase):
    def setUp(self):
        # Create Users
        self.user1 = User.objects.create_user(
            email="user1@example.com",
            username="user1@example.com",
            password="password123",
            fpl_id="12345",
        )
        self.user2 = User.objects.create_user(
            email="user2@example.com",
            username="user2@example.com",
            password="password123",
        )

        # Create Teams
        self.team1 = Team.objects.create(
            fpl_team_id="12345",
            team_name="Team One",
            manager_name="Manager One",
            current_total_points=100,
        )

        # Link Team to User
        self.user1.team = self.team1
        self.user1.save()

        # Create MiniLeague
        self.league1 = MiniLeague.objects.create(
            fpl_league_id="999", name="Test League", league_type="x", scoring_type="c"
        )

        # Link Team to League
        self.ml_team1 = MiniLeagueTeam.objects.create(
            mini_league=self.league1,
            team=self.team1,
            current_rank=1,
            total_points=100
        )

        # Grant User Access to League
        UserMiniLeague.objects.create(
            user=self.user1,
            mini_league=self.league1
        )

        # Authenticate User1
        self.token = Token.objects.create(user=self.user1)
        self.client.credentials(HTTP_AUTHORIZATION="Token " + self.token.key)

    def test_dashboard_view(self):
        url = reverse("api:dashboard")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Check expected keys
        self.assertIn("user", response.data)
        self.assertIn("team", response.data)
        self.assertIn("best_league_rank", response.data)
        self.assertEqual(response.data["best_league_rank"], 1)

    def test_leagues_view(self):
        url = reverse("api:leagues")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should contain at least one league
        self.assertTrue(len(response.data) > 0)
        self.assertEqual(response.data[0]["name"], "Test League")

    def test_league_detail_view(self):
        url = reverse("api:league_detail", kwargs={"league_id": self.league1.id})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["name"], "Test League")

    def test_league_detail_no_access(self):
        # Authenticate as User2 (who has no team/league)
        self.client.force_authenticate(user=self.user2)
        url = reverse("api:league_detail", kwargs={"league_id": self.league1.id})
        response = self.client.get(url)
        # User 2 is not in the league, so should get 403 Forbidden
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_team_detail_view(self):
        url = reverse("api:team_detail", kwargs={"team_id": self.team1.id})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["team_name"], "Team One")

    def test_me_view(self):
        url = reverse("api:me")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["email"], "user1@example.com")
