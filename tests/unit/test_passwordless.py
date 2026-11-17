import re
from io import StringIO
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount, SocialLogin
from django.contrib.auth import authenticate, get_user_model
from asgiref.sync import async_to_sync
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from rest_framework.authtoken.models import Token

from accounts.models import SignInLink
from accounts.onboarding import has_fpl_setup
from accounts.passwordless import issue_link, redeem_link, token_digest
from rivals.models import Team

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture(autouse=True)
def passwordless(settings):
    settings.PASSWORDLESS_AUTH_ENABLED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.SITE_URL = "http://testserver"
    settings.GOOGLE_CLIENT_ID = "google-client"
    settings.GOOGLE_CLIENT_SECRET = "google-secret"
    settings.SOCIALACCOUNT_PROVIDERS = {
        "google": {
            "OAUTH_PKCE_ENABLED": True,
            "APPS": [{"client_id": "google-client", "secret": "google-secret"}],
        }
    }


@pytest.fixture
def user():
    return User.objects.create_user(
        email="manager@example.com",
        username="legacy-manager",
        password="Legacy-password-123",
        is_verified=True,
    )


def connect_team(user):
    team = Team.objects.create(fpl_team_id="123", team_name="Test XI")
    user.fpl_id = "123"
    user.team = team
    user.save(update_fields=["team", "fpl_id"])
    return team


def emailed_path():
    return urlsplit(re.search(r"http://testserver/\S+", mail.outbox[-1].body)[0]).path


def confirm_sign_in(client):
    page = client.get(reverse("accounts:magic_confirm"))
    return client.post(
        reverse("accounts:magic_confirm"),
        {"challenge_id": page.context.get("challenge_id", "")},
    )


def test_email_request_defers_account_creation_and_requires_confirmation(client):
    response = client.post(reverse("accounts:login"), {"email": "New@Example.com"})
    assert response.status_code == 200
    assert not User.objects.exists()
    path = emailed_path()
    raw_secret = path.split("/")[-2]
    challenge = SignInLink.objects.get()
    assert challenge.digest == token_digest(raw_secret)
    assert raw_secret not in challenge.digest
    # An email scanner and a user may both open the original URL.
    scanner = Client()
    assert scanner.get(path, follow=True).status_code == 200
    challenge.refresh_from_db()
    assert challenge.used_at is None
    assert not User.objects.exists()
    response = client.get(path, follow=True)
    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]
    response = confirm_sign_in(client)
    assert response.url == reverse("accounts:onboarding")
    account = User.objects.get()
    assert account.email == "new@example.com"
    assert account.is_verified and not account.has_usable_password()
    assert EmailAddress.objects.get(user=account).verified
    assert confirm_sign_in(scanner).status_code == 400


def test_link_reuses_legacy_account_without_touching_fpl_data(client, user):
    team = connect_team(user)
    client.post(
        reverse("accounts:login"),
        {"email": user.email.upper(), "next": "/accounts/profile/"},
    )
    client.get(emailed_path())
    response = confirm_sign_in(client)
    assert response.url == "/accounts/profile/"
    assert User.objects.count() == 1
    user.refresh_from_db()
    assert user.username == "legacy-manager" and user.team_id == team.pk
    assert not user.has_usable_password()


def test_returning_passwordless_sign_in_keeps_other_device_session(client, user):
    connect_team(user)
    issue_link(user.email, "/dashboard/")
    client.get(emailed_path())
    confirm_sign_in(client)
    other_device = Client()
    issue_link(user.email, "/dashboard/")
    other_device.get(emailed_path())
    confirm_sign_in(other_device)
    assert other_device.get(reverse("api:me")).status_code == 200
    assert client.get(reverse("api:me")).status_code == 200


@pytest.mark.parametrize(
    "next_url",
    [
        "https://evil.example/",
        "//evil.example/",
        "///evil.example/",
        "javascript:alert(1)",
    ],
)
def test_link_rejects_external_redirects(client, user, next_url):
    connect_team(user)
    client.post(reverse("accounts:login"), {"email": user.email, "next": next_url})
    client.get(emailed_path())
    assert confirm_sign_in(client).url == reverse("rivals:dashboard")


def test_expired_link_and_disabled_account_cannot_sign_in(client, user):
    issue_link(user.email, "/dashboard/")
    path = emailed_path()
    SignInLink.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    client.get(path)
    assert confirm_sign_in(client).status_code == 400
    issue_link(user.email, "/dashboard/")
    client.get(emailed_path())
    user.is_active = False
    user.save(update_fields=["is_active"])
    assert confirm_sign_in(client).status_code == 400
    assert "_auth_user_id" not in client.session


def test_failed_delivery_does_not_leave_valid_link(client):
    with patch(
        "accounts.passwordless.send_mail", side_effect=RuntimeError("SMTP unavailable")
    ):
        response = client.post(reverse("accounts:login"), {"email": "new@example.com"})
    assert response.status_code == 200
    assert not SignInLink.objects.exists()
    assert not User.objects.exists()


def test_resend_is_limited_by_normalized_email_across_browser_and_api(client):
    assert (
        client.post(reverse("accounts:login"), {"email": "new@example.com"}).status_code
        == 200
    )
    assert (
        client.post(reverse("api:login"), {"email": "NEW@example.com"}).status_code
        == 429
    )
    assert len(mail.outbox) == 1


def test_redemption_requires_csrf():
    client = Client(enforce_csrf_checks=True)
    issue_link("new@example.com", "/dashboard/")
    client.get(emailed_path())
    assert confirm_sign_in(client).status_code == 403
    assert not User.objects.exists()


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    "route", ["accounts:login", "accounts:signup", "accounts:password_reset"]
)
def test_auth_forms_preserve_same_origin_referrers(client, settings, route, enabled):
    settings.PASSWORDLESS_AUTH_ENABLED = enabled
    response = client.get(reverse(route), follow=True)
    assert response.status_code == 200
    assert response["Referrer-Policy"] == "same-origin"
    assert "no-store" in response["Cache-Control"]


@pytest.mark.parametrize("secure", [False, True])
def test_email_login_accepts_same_origin_csrf_protected_post(settings, secure):
    settings.CSRF_TRUSTED_ORIGINS = []
    client = Client(enforce_csrf_checks=True)
    page = client.get(reverse("accounts:login"), secure=secure)
    assert page["Referrer-Policy"] == "same-origin"
    response = client.post(
        reverse("accounts:login"),
        {
            "email": "new@example.com",
            "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
        },
        secure=secure,
        HTTP_ORIGIN=f"{'https' if secure else 'http'}://testserver",
    )
    assert response.status_code == 200
    assert len(mail.outbox) == 1


@pytest.mark.parametrize("origin", ["null", "https://attacker.example"])
def test_email_login_rejects_untrusted_origin_with_valid_csrf_token(origin):
    client = Client(enforce_csrf_checks=True)
    client.get(reverse("accounts:login"))
    response = client.post(
        reverse("accounts:login"),
        {
            "email": "new@example.com",
            "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
        },
        HTTP_ORIGIN=origin,
    )
    assert response.status_code == 403
    assert not SignInLink.objects.exists()


def test_magic_link_hides_secret_referrer_and_confirmation_accepts_csrf_post():
    client = Client(enforce_csrf_checks=True)
    issue_link("new@example.com", "/dashboard/")
    response = client.get(emailed_path())
    assert response["Referrer-Policy"] == "no-referrer"
    page = client.get(response.url)
    assert page["Referrer-Policy"] == "same-origin"
    response = client.post(
        reverse("accounts:magic_confirm"),
        {
            "challenge_id": page.context["challenge_id"],
            "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
        },
        HTTP_ORIGIN="http://testserver",
    )
    assert response.status_code == 302
    assert "_auth_user_id" in client.session


@pytest.mark.parametrize("route", ["accounts:login", "accounts:signup"])
def test_auth_pages_have_no_password_fields(client, route):
    response = client.get(reverse(route))
    assert response.status_code == 200
    assert b'type="password"' not in response.content
    assert b"Continue with Google" in response.content


def test_password_entry_points_are_retired(client, user):
    assert authenticate(username=user.email, password="Legacy-password-123") is None
    for route in ("api:login", "api:signup"):
        assert (
            client.post(
                reverse(route), {"email": user.email, "password": "Legacy-password-123"}
            ).status_code
            == 410
        )
    assert not Token.objects.exists()
    assert client.get(reverse("accounts:password_reset")).url == reverse(
        "accounts:login"
    )
    assert client.get(
        reverse("accounts:password_reset_confirm", args=["MQ", "invalid"])
    ).url == reverse("accounts:login")
    assert client.get("/admin/login/").url.startswith(reverse("accounts:login"))


def test_email_api_starts_browser_session_flow_without_minting_token(client):
    response = client.post(reverse("api:login"), {"email": "new@example.com"})
    assert response.status_code == 202
    assert "token" not in response.json()
    client.get(emailed_path())
    confirm_sign_in(client)
    assert client.get(reverse("api:me")).json()["onboarding_required"] is True


@pytest.mark.parametrize(
    "route",
    ["rivals:dashboard", "rivals:refresh-status", "accounts:profile", "accounts:mcp"],
)
def test_incomplete_user_cannot_skip_browser_onboarding(client, user, route):
    client.force_login(user)
    response = client.get(reverse(route))
    assert response.status_code == 302
    assert response.url.startswith(reverse("accounts:onboarding"))


def test_htmx_gate_navigates_whole_page(client, user):
    client.force_login(user)
    response = client.get(reverse("rivals:dashboard"), HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    assert response["HX-Redirect"].startswith(reverse("accounts:onboarding"))


@pytest.mark.parametrize("auth", ["session", "token"])
def test_api_gate_applies_after_authentication(client, user, auth):
    headers = {}
    if auth == "session":
        client.force_login(user)
    else:
        headers["HTTP_AUTHORIZATION"] = f"Token {Token.objects.create(user=user).key}"
    for route in ("api:dashboard", "api:plans_list_create", "api:leagues"):
        response = client.get(reverse(route), **headers)
        assert response.status_code == 403
        assert response.json()["code"] == "onboarding_required"
    assert client.get(reverse("api:me"), **headers).status_code == 200


SNAPSHOT = {
    "id": 123,
    "name": "Test XI",
    "player_first_name": "Test",
    "player_last_name": "Manager",
    "summary_overall_points": 100,
    "summary_overall_rank": 1000,
    "leagues": {"classic": []},
}


def test_onboarding_publishes_setup_before_returning_to_saved_next(client, user):
    client.force_login(user)
    client.get(reverse("accounts:onboarding"), {"next": "/accounts/profile/"})
    with patch(
        "accounts.forms.FplApiService.fetch_team_basic_info", return_value=SNAPSHOT
    ) as fetch:
        response = client.post(reverse("accounts:onboarding"), {"fpl_id": "123"})
    assert fetch.call_count == 1
    assert response.url == "/accounts/profile/"
    user.refresh_from_db()
    assert has_fpl_setup(user) and user.fpl_revision == 1
    assert user.manager_name == "Test Manager"
    assert client.get(reverse("accounts:profile")).status_code == 200
    assert client.get(reverse("api:dashboard")).status_code == 200


@pytest.mark.parametrize("result", [None, {"id": 321}, requests.Timeout()])
def test_fpl_validation_failure_preserves_incomplete_state(client, user, result):
    client.force_login(user)
    with patch(
        "accounts.forms.FplApiService.fetch_team_basic_info",
        **(
            {"side_effect": result}
            if isinstance(result, Exception)
            else {"return_value": result}
        ),
    ):
        response = client.post(reverse("accounts:onboarding"), {"fpl_id": "123"})
    assert response.status_code == 200
    user.refresh_from_db()
    assert not has_fpl_setup(user) and user.fpl_revision == 0


def test_fpl_initialization_failure_rolls_back_team_change(client, user):
    original = connect_team(user)
    client.force_login(user)
    snapshot = {**SNAPSHOT, "id": 456}
    with (
        patch(
            "accounts.forms.FplApiService.fetch_team_basic_info", return_value=snapshot
        ),
        patch("accounts.onboarding.InitUserService.save_user_info", return_value=False),
    ):
        response = client.post(reverse("accounts:update_fpl_id"), {"fpl_id": "456"})
    assert response.status_code == 200
    user.refresh_from_db()
    assert (
        user.team_id == original.pk and user.fpl_id == "123" and user.fpl_revision == 0
    )


def test_api_can_complete_onboarding(client, user):
    client.force_login(user)
    with patch(
        "accounts.forms.FplApiService.fetch_team_basic_info", return_value=SNAPSHOT
    ):
        response = client.post(reverse("api:onboarding"), {"fpl_id": "123"})
    assert response.status_code == 200
    assert response.json()["onboarding_required"] is False


def google_callback(client, email, uid="google-user", verified=True, process="login"):
    start = client.post(reverse("google_login") + f"?process={process}")
    assert start.status_code == 302
    params = parse_qs(urlsplit(start.url).query)
    assert params["code_challenge_method"] == ["S256"]
    sociallogin = SocialLogin(
        user=User(email=email),
        account=SocialAccount(provider="google", uid=uid),
        email_addresses=[EmailAddress(email=email, verified=verified, primary=True)],
    )

    def complete_login(adapter, request, app, token, **kwargs):
        sociallogin.provider = adapter.get_provider()
        return sociallogin

    with (
        patch(
            "allauth.socialaccount.providers.oauth2.client.OAuth2Client.get_access_token",
            return_value={"access_token": "provider-token"},
        ),
        patch(
            "allauth.socialaccount.providers.google.views.GoogleOAuth2Adapter.complete_login",
            autospec=True,
            side_effect=complete_login,
        ),
    ):
        return client.get(
            reverse("google_callback"),
            {"code": "provider-code", "state": params["state"][0]},
        )


def test_google_signup_creates_verified_account_and_requires_fpl(client):
    response = google_callback(client, "google@example.com")
    assert response.status_code == 302
    assert response.url == reverse("accounts:onboarding")
    account = User.objects.get()
    assert account.is_verified and not account.has_usable_password()
    assert EmailAddress.objects.get(user=account).verified
    assert SocialAccount.objects.get().uid == "google-user"


def test_google_matching_email_does_not_take_over_existing_account(client, user):
    response = google_callback(client, user.email)
    assert response.url == reverse("accounts:login")
    assert User.objects.count() == 1
    assert not SocialAccount.objects.exists()
    assert "_auth_user_id" not in client.session


def test_google_unverified_email_cannot_create_account(client):
    response = google_callback(client, "google@example.com", verified=False)
    assert response.url == reverse("accounts:login")
    assert not User.objects.exists()


def test_google_connect_then_return_and_disconnect(client, user):
    connect_team(user)
    client.force_login(user)
    response = google_callback(client, user.email, process="connect")
    assert response.url == reverse("accounts:connections")
    account = SocialAccount.objects.get(user=user)
    client.logout()
    response = google_callback(client, user.email)
    assert response.url == reverse("rivals:dashboard")
    assert client.session["_auth_user_id"] == str(user.pk)
    response = client.post(reverse("accounts:connections"), {"account": account.pk})
    assert response.status_code == 302 and not SocialAccount.objects.exists()


def test_email_uniqueness_ignores_case(user):
    with pytest.raises(IntegrityError), transaction.atomic():
        User.objects.create_user(username="other", email=user.email.upper())


def test_google_cancel_and_forged_callback_do_not_authenticate(client):
    start = client.post(reverse("google_login"))
    state = parse_qs(urlsplit(start.url).query)["state"][0]
    response = client.get(
        reverse("google_callback"), {"error": "access_denied", "state": state}
    )
    assert response.url == reverse("accounts:login")
    response = client.get(
        reverse("google_callback"), {"code": "forged", "state": "invalid"}
    )
    assert response.status_code in (302, 400)
    assert "_auth_user_id" not in client.session
    assert not User.objects.exists()


def test_google_confirmation_page_uses_application_layout(client):
    response = client.get(reverse("google_login"))
    assert response.status_code == 200
    assert b"Continue with Google" in response.content
    assert "no-store" in response["Cache-Control"]
    assert response["Referrer-Policy"] == "same-origin"


def test_staff_email_sign_in_returns_to_admin_without_fpl(client, user):
    user.is_staff = True
    user.save(update_fields=["is_staff"])
    client.post(reverse("accounts:login"), {"email": user.email, "next": "/admin/"})
    client.get(emailed_path())
    assert confirm_sign_in(client).url == "/admin/"
    assert client.get(reverse("rivals:dashboard")).url.startswith(
        reverse("accounts:onboarding")
    )


def test_admin_login_denies_non_staff_without_redirect_loop(client, user):
    connect_team(user)
    client.force_login(user)
    assert client.get("/admin/login/", follow=True).status_code == 403


def test_password_retirement_requires_flag_and_explicit_apply(settings, user):
    output = StringIO()
    call_command("retire_passwords", stdout=output)
    user.refresh_from_db()
    assert (
        user.has_usable_password()
        and "Would retire passwords for 1" in output.getvalue()
    )
    call_command("retire_passwords", apply=True, stdout=StringIO())
    user.refresh_from_db()
    assert not user.has_usable_password()
    settings.PASSWORDLESS_AUTH_ENABLED = False
    with pytest.raises(CommandError):
        call_command("retire_passwords", apply=True)


def test_api_rejects_malformed_body_and_ignores_non_string_next(client):
    assert (
        client.post(
            reverse("api:login"), '["unexpected"]', content_type="application/json"
        ).status_code
        == 400
    )
    response = client.post(
        reverse("api:login"),
        {"email": "new@example.com", "next": {"bad": "value"}},
        content_type="application/json",
    )
    assert response.status_code == 202
    assert SignInLink.objects.get().redirect_to == reverse("rivals:dashboard")


def test_mcp_consent_preserves_authorization_request_through_setup(
    client, user, settings
):
    settings.MCP_ENABLED = True
    client.force_login(user)
    target = "/mcp/oauth/authorize/?client_id=test&state=client-state&resource=http%3A%2F%2Ftestserver%2Fmcp"
    response = client.get(target)
    assert response.url.startswith(reverse("accounts:onboarding"))
    client.get(response.url)
    with patch(
        "accounts.forms.FplApiService.fetch_team_basic_info", return_value=SNAPSHOT
    ):
        response = client.post(reverse("accounts:onboarding"), {"fpl_id": "123"})
    assert response.url == target


def test_existing_mcp_grant_requires_fpl_setup(user):
    from oauth2_provider.models import AccessToken, Application
    from rivals_mcp.oauth import resource_url
    from rivals_mcp.server import authenticate as authenticate_mcp

    app = Application.objects.create(
        name="Assistant",
        client_type="public",
        authorization_grant_type="authorization-code",
    )
    AccessToken.objects.create(
        user=user,
        application=app,
        token="existing-mcp-token",
        scope="rivals:read",
        resource=[resource_url()],
        expires=timezone.now() + timedelta(hours=1),
    )
    assert authenticate_mcp("existing-mcp-token", "127.0.0.1") == (
        "onboarding_required",
        403,
    )


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("verified", [False, True])
def test_dashboard_gate_preserves_legacy_verification_rules(
    client, user, settings, enabled, verified
):
    settings.PASSWORDLESS_AUTH_ENABLED = enabled
    user.is_verified = verified
    user.save(update_fields=["is_verified"])
    client.force_login(user)
    response = client.get(reverse("api:dashboard"))
    if verified and not enabled:
        assert response.status_code == 200
        assert response.json()["team"] is None
        assert response.json()["user"]["onboarding_required"] is True
    else:
        assert response.status_code == 403
        if verified:
            assert response.json()["code"] == "onboarding_required"


@pytest.mark.parametrize("enabled", [False, True])
def test_async_password_auth_obeys_rollout_flag(user, settings, enabled):
    from django.contrib.auth import aauthenticate

    settings.PASSWORDLESS_AUTH_ENABLED = enabled
    result = async_to_sync(aauthenticate)(
        username=user.email, password="Legacy-password-123"
    )
    assert (result is not None) is (not enabled)


@pytest.mark.parametrize("disabled_before_start", [False, True])
@pytest.mark.parametrize("existing_account", [False, True])
def test_google_pending_callback_cannot_sign_in_after_credentials_removed(
    client, settings, disabled_before_start, existing_account
):
    if existing_account:
        account = User.objects.create_user(
            email="google@example.com", username="linked-google-user", is_verified=True
        )
        SocialAccount.objects.create(
            user=account, provider="google", uid="disabled-google-user"
        )
    if disabled_before_start:
        settings.GOOGLE_CLIENT_ID = ""
        settings.GOOGLE_CLIENT_SECRET = ""
    start = client.post(reverse("google_login"))
    state = parse_qs(urlsplit(start.url).query)["state"][0]
    settings.GOOGLE_CLIENT_ID = ""
    settings.GOOGLE_CLIENT_SECRET = ""

    def complete_login(adapter, request, app, token, **kwargs):
        return SocialLogin(
            provider=adapter.get_provider(),
            user=User(email="google@example.com"),
            account=SocialAccount(provider="google", uid="disabled-google-user"),
            email_addresses=[
                EmailAddress(email="google@example.com", verified=True, primary=True)
            ],
        )

    with (
        patch(
            "allauth.socialaccount.providers.oauth2.client.OAuth2Client.get_access_token",
            return_value={"access_token": "provider-token"},
        ),
        patch(
            "allauth.socialaccount.providers.google.views.GoogleOAuth2Adapter.complete_login",
            autospec=True,
            side_effect=complete_login,
        ),
    ):
        response = client.get(
            reverse("google_callback"), {"code": "provider-code", "state": state}
        )
    assert response.url == reverse("accounts:login")
    assert "_auth_user_id" not in client.session
    assert User.objects.count() == int(existing_account)


@pytest.mark.parametrize("credentials_present", [False, True])
def test_email_only_login_does_not_reverse_optional_google_route(
    client, settings, credentials_present
):
    from django.urls import NoReverseMatch

    if not credentials_present:
        settings.GOOGLE_CLIENT_ID = ""
        settings.GOOGLE_CLIENT_SECRET = ""
    actual_reverse = reverse

    def without_google(name, *args, **kwargs):
        if name == "google_login":
            raise NoReverseMatch("Google provider is not registered")
        return actual_reverse(name, *args, **kwargs)

    with patch("accounts.passwordless_views.reverse", side_effect=without_google):
        response = client.get(reverse("accounts:login"))
    assert response.status_code == 200
    assert b"Email me a sign-in link" in response.content
    assert b"Continue with Google" not in response.content


def test_stale_confirmation_cannot_sign_in_as_latest_shared_session_challenge(client):
    issue_link("first@example.com", "/dashboard/")
    first_path = emailed_path()
    first = client.get(first_path, follow=True)
    first_id = first.context.get("challenge_id", "missing-first-id")
    issue_link("second@example.com", "/dashboard/")
    second_path = emailed_path()
    second = client.get(second_path, follow=True)
    second_id = second.context.get("challenge_id", "missing-second-id")
    digest = client.session["sign_in_digest"]
    response = client.post(
        reverse("accounts:magic_confirm"), {"challenge_id": first_id}
    )
    assert response.status_code == 400
    assert not User.objects.exists()
    assert client.session["sign_in_digest"] == digest
    assert SignInLink.objects.filter(used_at__isnull=True).count() == 2
    response = client.post(
        reverse("accounts:magic_confirm"), {"challenge_id": second_id}
    )
    assert response.url == reverse("accounts:onboarding")
    assert User.objects.get().email == "second@example.com"


def test_confirmation_without_challenge_id_does_not_consume_link(client):
    issue_link("first@example.com", "/dashboard/")
    client.get(emailed_path())
    digest = client.session["sign_in_digest"]
    assert client.post(reverse("accounts:magic_confirm")).status_code == 400
    assert not User.objects.exists()
    assert client.session["sign_in_digest"] == digest
    assert SignInLink.objects.get().used_at is None


def test_rollback_password_recovery_includes_unusable_passwords(client, user, settings):
    settings.PASSWORDLESS_AUTH_ENABLED = False
    user.set_unusable_password()
    user.save(update_fields=["password"])
    response = client.post(reverse("accounts:password_reset"), {"email": user.email})
    assert response.status_code == 302
    assert len(mail.outbox) == 1
    reset_path = emailed_path()
    response = client.get(reset_path, follow=True)
    assert response.status_code == 200
    response = client.post(
        response.request["PATH_INFO"],
        {
            "new_password1": "Recovered-password-123!",
            "new_password2": "Recovered-password-123!",
        },
    )
    assert response.status_code == 302
    user.refresh_from_db()
    assert user.check_password("Recovered-password-123!")


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("denial", ["onboarding_required", "scope"])
def test_mcp_transport_distinguishes_setup_from_scope(settings, denial):
    from starlette.testclient import TestClient
    from rivals_mcp.server import create_app

    settings.MCP_ENABLED = True
    settings.ALLOWED_HOSTS = ["testserver"]
    identity = "onboarding_required" if denial == "onboarding_required" else None
    with (
        patch("rivals_mcp.server.authenticate", return_value=(identity, 403)),
        TestClient(create_app()) as http,
    ):
        response = http.get("/mcp", headers={"Authorization": "Bearer test-token"})
    assert response.status_code == 403
    if denial == "onboarding_required":
        assert response.json()["code"] == "onboarding_required"
        assert (
            response.json()["onboarding_url"]
            == "http://testserver/accounts/onboarding/fpl/"
        )
        assert "complete" in response.json()["error"].lower()
        assert "insufficient_scope" not in response.headers.get("WWW-Authenticate", "")
    else:
        assert response.json()["error"] == "Read permission required"
        assert "insufficient_scope" in response.headers["WWW-Authenticate"]
