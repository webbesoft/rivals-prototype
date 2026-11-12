from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

User = get_user_model()


@pytest.mark.django_db
class TestUserModel:
    def test_create_user(self):
        user = User.objects.create_user(
            email="test@example.com",
            username="test@example.com",
            password="testpass123",
            first_name="Test",
            last_name="User",
        )
        assert user.email == "test@example.com"
        assert user.is_verified is False
        assert user.check_password("testpass123")


@pytest.mark.django_db
class TestAuthViews:
    @patch("rivals.services.fpl_api_service.FplApiService.fetch_team_basic_info")
    @patch("accounts.tasks.init_user_data_task")
    def test_signup_view(self, mock_init_task, mock_fetch_team):
        mock_fetch_team.return_value = {
            "id": 12345,
            "name": "FC Test",
            "player_first_name": "New",
            "player_last_name": "User",
            "summary_overall_points": 100,
            "summary_overall_rank": 1000,
        }
        client = Client()
        response = client.post(
            reverse("accounts:signup"),
            {
                "fpl_id": 12345,
                "email": "newuser@example.com",
                "password1": "securepass123",
                "password2": "securepass123",
            },
        )
        assert response.status_code == 302
        assert User.objects.filter(email="newuser@example.com").exists()
