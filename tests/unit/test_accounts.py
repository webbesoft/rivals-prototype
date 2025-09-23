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
    def test_signup_view(self):
        client = Client()
        response = client.post(
            reverse("accounts:signup"),
            {
                "email": "newuser@example.com",
                "password1": "securepass123",
                "password2": "securepass123",
                "first_name": "New",
                "last_name": "User",
            },
        )
        assert response.status_code == 302
        assert User.objects.filter(email="newuser@example.com").exists()
