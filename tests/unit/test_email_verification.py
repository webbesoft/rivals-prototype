import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.test import Client, override_settings
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

User = get_user_model()


@pytest.mark.django_db
@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    SITE_URL="http://testserver",
)
def test_signup_sends_verification_email(monkeypatch):
    # Patch external services used during signup
    class DummyFPL:
        def fetch_team_basic_info(self, fpl_id):
            return {
                "name": "Dummy Team",
                "player_first_name": "First",
                "player_last_name": "Last",
            }

    class DummyInit:
        def __init__(self, user):
            pass

        def save_user_info(self, basic_info=None):
            return None

    monkeypatch.setattr(
        "rivals.services.fpl_api_service.FplApiService", lambda: DummyFPL()
    )
    monkeypatch.setattr(
        "rivals.services.init_user_service.InitUserService",
        lambda user: DummyInit(user),
    )

    client = Client()
    response = client.post(
        reverse("accounts:signup"),
        {
            "fpl_id": 12345,
            "email": "verifyme@example.com",
            "password1": "strongpass123",
            "password2": "strongpass123",
        },
    )

    assert response.status_code == 302
    assert User.objects.filter(email="verifyme@example.com").exists()

    # One verification email should have been sent
    assert len(mail.outbox) == 1
    sent = mail.outbox[0]
    assert "Verify your email" in sent.subject
    assert "/accounts/verify-email/" in sent.body


@pytest.mark.django_db
def test_verify_email_activates_user(client):
    user = User.objects.create_user(
        email="activate@example.com",
        username="activate@example.com",
        password="pass12345",
    )
    assert user.is_verified is False

    token = default_token_generator.make_token(user)
    uid = urlsafe_base64_encode(force_bytes(user.pk))

    res = client.get(reverse("accounts:verify_email", args=[uid, token]))
    assert res.status_code == 302

    user.refresh_from_db()
    assert user.is_verified is True


@pytest.mark.django_db
def test_login_blocked_before_verification(client):
    user = User.objects.create_user(
        email="blocked@example.com",
        username="blocked@example.com",
        password="blockme123",
    )
    assert user.is_verified is False

    res = client.post(
        reverse("accounts:login"),
        {"username": "blocked@example.com", "password": "blockme123"},
    )

    # Should render login page again with an error message
    assert res.status_code == 200
    assert b"not verified" in res.content.lower()
