"""Exercise real WebAuthn challenges/signatures without a physical authenticator."""

import hashlib
import json
import re
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from html.parser import HTMLParser
from threading import Barrier
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from allauth.mfa.models import Authenticator
from allauth.mfa.webauthn.internal.auth import STATE_SESSION_KEY
from allauth.mfa.webauthn.internal import auth
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.db import connections
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from fido2.cose import ES256
from fido2.utils import websafe_encode
from fido2.webauthn import (
    AttestationObject,
    AttestedCredentialData,
    AuthenticatorData,
    CollectedClientData,
)

from accounts.passwordless import issue_link
from accounts.passkey_views import PasskeyLoginView
from accounts.models import PasskeyChallenge
from rivals.models import Team
from tests.unit.test_passwordless import google_callback

pytestmark = pytest.mark.django_db
User = get_user_model()
ORIGIN = "https://testserver"
FLAGS = AuthenticatorData.FLAG


class HiddenInputs(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.values = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("type") == "hidden" and "name" in attrs:
            self.values[attrs["name"]] = attrs.get("value", "")


@pytest.fixture(autouse=True)
def passwordless(settings):
    settings.PASSWORDLESS_AUTH_ENABLED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.SITE_URL = ORIGIN
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def user():
    return User.objects.create_user(
        email="passkey@example.com", username="passkey-manager", is_verified=True
    )


def sign_in(client, user, next_url=None):
    issue_link(user.email, next_url or reverse("mfa_add_webauthn"))
    return confirm_email(client)


def confirm_email(client):
    path = urlsplit(re.search(r"https://testserver/\S+", mail.outbox[-1].body)[0]).path
    page = client.get(path, follow=True, secure=True)
    return client.post(
        reverse("accounts:magic_confirm"),
        {"challenge_id": page.context["challenge_id"]},
        secure=True,
    )


class Device:
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = secrets.token_bytes(32)

    def register(self, options, *, origin=ORIGIN, verified=True):
        data = CollectedClientData.create(
            "webauthn.create", options["publicKey"]["challenge"], origin
        )
        credential = AttestedCredentialData.create(
            bytes(16),
            self.credential_id,
            ES256.from_cryptography_key(self.key.public_key()),
        )
        auth_data = AuthenticatorData.create(
            hashlib.sha256(b"testserver").digest(),
            FLAGS.UP | FLAGS.AT | (FLAGS.UV if verified else 0),
            0,
            credential,
        )
        return {
            "id": websafe_encode(self.credential_id),
            "rawId": websafe_encode(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": websafe_encode(data),
                "attestationObject": websafe_encode(
                    AttestationObject.create("none", auth_data, {})
                ),
            },
            "clientExtensionResults": {"credProps": {"rk": True}},
        }

    def authenticate(self, options, user, *, origin=ORIGIN, verified=True):
        data = CollectedClientData.create(
            "webauthn.get", options["publicKey"]["challenge"], origin
        )
        auth_data = AuthenticatorData.create(
            hashlib.sha256(b"testserver").digest(),
            FLAGS.UP | (FLAGS.UV if verified else 0),
            1,
        )
        signature = self.key.sign(auth_data + data.hash, ec.ECDSA(hashes.SHA256()))
        return {
            "id": websafe_encode(self.credential_id),
            "rawId": websafe_encode(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": websafe_encode(data),
                "authenticatorData": websafe_encode(auth_data),
                "signature": websafe_encode(signature),
                "userHandle": websafe_encode(str(user.pk).encode()),
            },
            "clientExtensionResults": {},
        }


def register(client, user):
    sign_in(client, user)
    page = client.get(reverse("mfa_add_webauthn"), secure=True)
    assert page.status_code == 200
    # Submit the rendered hidden defaults, just as a user who only edits the
    # name would. A hand-written passwordless=True would hide UI regressions.
    data = HiddenInputs(page.content.decode()).values
    assert data["passwordless"] == "True"
    device = Device()
    response = client.post(
        reverse("mfa_add_webauthn"),
        {
            **data,
            "name": "My laptop",
            "credential": json.dumps(
                device.register(page.context["js_data"]["creation_options"])
            ),
        },
        secure=True,
    )
    assert response.status_code == 302, (
        response.context and response.context.get("form").errors
    )
    assert response.url == reverse("accounts:connections")
    return device


@pytest.mark.parametrize("method", ["get", "post"])
def test_registration_starts_one_passwordless_ceremony_with_bound_form(
    client, user, method
):
    sign_in(client, user)
    with patch(
        "accounts.passkey_views.auth.begin_registration", wraps=auth.begin_registration
    ) as begin:
        response = getattr(client, method)(
            reverse("mfa_add_webauthn"),
            {"name": "Keep my name", "passwordless": "True", "credential": "invalid"},
            secure=True,
        )
    assert response.status_code == 200
    begin.assert_called_once_with(user, True)
    assert response.context["form"].is_bound is (method == "post")
    if method == "post":
        assert response.context["form"].data["name"] == "Keep my name"
        assert response.context["form"].errors


def test_email_confirmation_allows_first_passkey_without_existing_email_row(
    client, user
):
    sign_in(client, user)
    user.emailaddress_set.all().delete()
    session = client.session
    for record in session["account_authentication_methods"]:
        record["at"] -= 301
    session.save()
    assert not user.emailaddress_set.exists()
    assert not Authenticator.objects.filter(user=user).exists()
    response = client.get(reverse("mfa_add_webauthn"), secure=True)
    assert response.url.startswith(reverse("accounts:reauthenticate"))
    assert client.post(response.url, secure=True).status_code == 200
    confirm_email(client)
    assert user.emailaddress_set.get().verified
    assert client.get(reverse("mfa_add_webauthn"), secure=True).status_code == 200


def test_reauthentication_delivery_error_preserves_form_and_destination(client, user):
    client.force_login(user)
    target = reverse("mfa_add_webauthn")
    with patch(
        "accounts.passwordless_views.issue_link",
        side_effect=RuntimeError("SMTP unavailable"),
    ):
        response = client.post(
            reverse("accounts:reauthenticate"), {"next": target}, secure=True
        )
    assert response.status_code == 200
    assert response.context["next"] == target
    assert b"Could not send" in response.content
    assert b"Email me a confirmation link" in response.content
    assert b"SMTP unavailable" not in response.content
    assert (
        client.post(
            reverse("accounts:reauthenticate"), {"next": target}, secure=True
        ).status_code
        == 429
    )
    from core.models import RequestLimit

    RequestLimit.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    retry = client.post(
        reverse("accounts:reauthenticate"), {"next": target}, secure=True
    )
    assert retry.status_code == 200
    assert b"Check your inbox" in retry.content


@pytest.mark.django_db(transaction=True)
def test_concurrent_assertions_allow_only_one_sign_in(client, user, tmp_path):
    # Match production's file-backed SQLite and busy timeout, rather than
    # shared-memory SQLite's immediate SQLITE_LOCKED behavior.
    original_db = connections["default"]
    database = original_db.copy(alias="default")
    database.settings_dict = {
        **database.settings_dict,
        "NAME": str(tmp_path / "passkeys.sqlite3"),
    }
    database.ensure_connection()
    original_db.connection.backup(database.connection)
    connections["default"] = database
    try:
        _concurrent_assertions(client, user, database)
    finally:
        database.close()
        connections["default"] = original_db


def _concurrent_assertions(client, user, database):
    device = register(client, user)
    client.logout()
    credential, challenge_id = login_credential(client, user, device)
    credential = json.dumps(credential)
    barrier = Barrier(2)
    original = PasskeyLoginView.get_form

    def synchronized_form(view, *args, **kwargs):
        form = original(view, *args, **kwargs)
        assert STATE_SESSION_KEY in view.request.session
        barrier.wait(timeout=10)
        return form

    def submit():
        local = database.copy(alias="default")
        connections["default"] = local
        browser = Client()
        browser.cookies.update(client.cookies)
        try:
            response = browser.post(
                reverse("mfa_login_webauthn"),
                {"credential": credential, "challenge_id": challenge_id},
                secure=True,
            )
            return response.status_code == 302 and browser.session.get(
                "_auth_user_id"
            ) == str(user.pk)
        finally:
            connections.close_all()

    with (
        patch.object(PasskeyLoginView, "get_form", synchronized_form),
        patch(
            "accounts.passkey_views.auth.complete_authentication",
            wraps=auth.complete_authentication,
        ) as verify,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        results = list(pool.map(lambda _: submit(), range(2)))
    assert sum(results) == 1
    assert verify.call_count == 1


def test_enrollment_tabs_have_independent_single_use_challenges(client, user):
    sign_in(client, user)
    url = reverse("mfa_add_webauthn")
    pages = [client.get(url, secure=True), client.get(url, secure=True)]
    payloads = []
    for index, page in enumerate(pages):
        data = HiddenInputs(page.content.decode()).values
        assert data["challenge_id"] == page.context["js_data"]["challenge_id"]
        data.update(
            name=f"Device {index}",
            credential=json.dumps(
                Device().register(page.context["js_data"]["creation_options"])
            ),
        )
        payloads.append(data)
    assert payloads[0]["challenge_id"] != payloads[1]["challenge_id"]
    for data in payloads:
        assert client.post(url, data, secure=True).status_code == 302
    assert Authenticator.objects.filter(user=user).count() == 2
    assert client.post(url, payloads[0], secure=True).status_code == 200
    assert Authenticator.objects.filter(user=user).count() == 2
    assert (
        PasskeyChallenge.objects.get(pk=payloads[0]["challenge_id"]).used_at is not None
    )


@pytest.mark.parametrize(
    "invalid", ["expired", "other-session", "other-user", "wrong-purpose", "missing-id"]
)
def test_enrollment_rejects_invalid_challenge_before_verification(
    client, user, invalid
):
    sign_in(client, user)
    url = reverse("mfa_add_webauthn")
    page = client.get(url, secure=True)
    data = HiddenInputs(page.content.decode()).values
    data["credential"] = json.dumps(
        Device().register(page.context["js_data"]["creation_options"])
    )
    challenge = PasskeyChallenge.objects.get(pk=data["challenge_id"])
    browser = client
    if invalid == "expired":
        challenge.expires_at = timezone.now() - timedelta(seconds=1)
        challenge.save(update_fields=["expires_at"])
    elif invalid == "other-session":
        browser = Client()
        sign_in(browser, user)
    elif invalid == "other-user":
        challenge.user = User.objects.create_user(
            email="other@example.com", username="other"
        )
        challenge.save(update_fields=["user"])
    elif invalid == "wrong-purpose":
        challenge.purpose = PasskeyChallenge.Purpose.LOGIN
        challenge.save(update_fields=["purpose"])
    else:
        data.pop("challenge_id")
    with patch(
        "accounts.passkey_views.auth.complete_registration",
        wraps=auth.complete_registration,
    ) as verify:
        response = browser.post(url, data, secure=True)
    assert response.status_code == 200
    assert response.context["form"].errors
    assert verify.call_count == 0
    assert not Authenticator.objects.filter(user=user).exists()


def test_expired_login_challenge_cannot_be_verified(client, user):
    device = register(client, user)
    client.logout()
    credential, challenge_id = login_credential(client, user, device)
    PasskeyChallenge.objects.filter(pk=challenge_id).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    with patch(
        "accounts.passkey_views.auth.complete_authentication",
        wraps=auth.complete_authentication,
    ) as verify:
        response = client.post(
            reverse("mfa_login_webauthn"),
            {"credential": json.dumps(credential), "challenge_id": challenge_id},
            secure=True,
        )
    assert response.url == reverse("accounts:login")
    assert "_auth_user_id" not in client.session
    assert verify.call_count == 0


@pytest.mark.parametrize("method", ["email", "google"])
def test_registered_passkey_preserves_other_sign_in_methods_in_fresh_session(
    client, user, settings, method
):
    user.email = "passkey@gmail.com"
    user.team = Team.objects.create(fpl_team_id="123", team_name="My XI")
    user.fpl_id = "123"
    user.save(update_fields=["email", "team", "fpl_id"])
    register(client, user)
    key = Authenticator.objects.get(user=user)
    assert key.wrap().is_passwordless is True

    browser = Client()
    assert not browser.cookies
    if method == "email":
        response = sign_in(browser, user, reverse("rivals:dashboard"))
    else:
        settings.GOOGLE_CLIENT_ID = "google-client"
        settings.GOOGLE_CLIENT_SECRET = "google-secret"
        settings.SOCIALACCOUNT_PROVIDERS = {
            "google": {
                "EMAIL_AUTHENTICATION": True,
                "OAUTH_PKCE_ENABLED": True,
                "APPS": [{"client_id": "google-client", "secret": "google-secret"}],
            }
        }
        response = google_callback(browser, user.email)

    # The full callback must establish a session, without an MFA stage or a
    # reverse of the intentionally unregistered mfa_authenticate URL.
    assert response.status_code == 302
    assert response.url == reverse("rivals:dashboard")
    assert browser.session["_auth_user_id"] == str(user.pk)
    assert browser.get(reverse("accounts:connections")).status_code == 200
    key.refresh_from_db()
    assert key.last_used_at is None  # No passkey challenge was used for login.


def login_credential(client, user, device, **kwargs):
    response = client.get(
        reverse("mfa_login_webauthn"), HTTP_ACCEPT="application/json", secure=True
    )
    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]
    data = response.json()
    return (
        device.authenticate(data["request_options"], user, **kwargs),
        data["challenge_id"],
    )


def test_passkey_sign_in_survives_browser_restart(client, user, settings):
    device = register(client, user)
    client = Client()
    credential, challenge_id = login_credential(client, user, device)
    response = client.post(
        reverse("mfa_login_webauthn"),
        {"credential": json.dumps(credential), "challenge_id": challenge_id},
        secure=True,
    )
    assert response.status_code == 302
    reopened = Client()
    for name, cookie in client.cookies.items():
        if cookie["expires"] or cookie["max-age"]:
            reopened.cookies[name] = cookie.value
    assert reopened.get(reverse("api:me"), secure=True).status_code == 200
    cookie = response.cookies[settings.SESSION_COOKIE_NAME]
    assert cookie["expires"]
    assert int(cookie["max-age"]) == settings.SESSION_COOKIE_AGE


def test_passkey_registration_login_redirect_and_removal(client, user):
    device = register(client, user)
    key = Authenticator.objects.get(user=user)
    assert str(key) == "My laptop"
    assert key.last_used_at is None
    team = Team.objects.create(fpl_team_id="123", team_name="My XI")
    user.fpl_id, user.team = "123", team
    user.save(update_fields=["fpl_id", "team"])
    methods = client.get(reverse("accounts:connections"), secure=True)
    assert b"My laptop" in methods.content
    assert b'aria-label="Remove My laptop"' in methods.content
    client = Client()
    credential, challenge_id = login_credential(client, user, device)
    response = client.post(
        reverse("mfa_login_webauthn"),
        {
            "credential": json.dumps(credential),
            "challenge_id": challenge_id,
            "next": reverse("accounts:profile"),
        },
        secure=True,
    )
    assert response.url == reverse("accounts:profile")
    assert client.session["_auth_user_id"] == str(user.pk)
    key.refresh_from_db()
    assert key.last_used_at is not None
    # A successful passkey sign-in is recent enough to manage passkeys.
    remove_url = reverse("mfa_remove_webauthn", args=[key.pk])
    assert client.get(remove_url, secure=True).status_code == 200
    assert Authenticator.objects.filter(pk=key.pk).exists()
    response = client.post(remove_url, secure=True)
    assert response.url == reverse("accounts:connections")
    assert not Authenticator.objects.exists()
    # Last-key removal leaves email access intact.
    client.logout()
    assert sign_in(client, user).status_code == 302
    assert client.session["_auth_user_id"] == str(user.pk)


def test_registration_requires_recent_sign_in(client, user):
    response = client.get(reverse("mfa_add_webauthn"), secure=True)
    assert response.url.startswith(reverse("accounts:login"))
    client.force_login(user)
    response = client.get(reverse("mfa_add_webauthn"), secure=True)
    assert response.url.startswith(reverse("accounts:reauthenticate"))
    assert b"Confirm it" in client.get(response.url, secure=True).content
    response = client.post(response.url, {"email": "attacker@example.com"}, secure=True)
    assert response.status_code == 200
    assert mail.outbox[-1].to == [user.email]
    assert (
        client.post(reverse("accounts:reauthenticate"), secure=True).status_code == 429
    )
    assert not Authenticator.objects.exists()


def test_expired_sign_in_requires_confirmation_for_add_and_remove(client, user):
    user.team = Team.objects.create(fpl_team_id="123", team_name="My XI")
    user.fpl_id = "123"
    user.save(update_fields=["team", "fpl_id"])
    register(client, user)
    key = Authenticator.objects.get()
    session = client.session
    for record in session["account_authentication_methods"]:
        record["at"] -= 301
    session.save()
    for url in [
        reverse("mfa_add_webauthn"),
        reverse("mfa_remove_webauthn", args=[key.pk]),
    ]:
        assert client.get(url, secure=True).url.startswith(
            reverse("accounts:reauthenticate")
        )
    assert Authenticator.objects.filter(pk=key.pk).exists()
    remove_url = reverse("mfa_remove_webauthn", args=[key.pk])
    response = client.post(remove_url, secure=True)
    assert "account_reauthentication_state" in client.session
    assert client.post(response.url, secure=True).status_code == 200
    assert confirm_email(client).url == remove_url
    assert "account_reauthentication_state" not in client.session
    # Email confirmation returns to the form without replaying the old delete.
    assert Authenticator.objects.filter(pk=key.pk).exists()
    assert client.get(remove_url, secure=True).status_code == 200
    assert client.post(remove_url, secure=True).url == reverse("accounts:connections")
    assert not Authenticator.objects.exists()


@pytest.mark.parametrize("bad", ["origin", "verification", "challenge", "passwordless"])
def test_invalid_registration_cannot_save_passkey(client, user, bad):
    sign_in(client, user)
    page = client.get(reverse("mfa_add_webauthn"), secure=True)
    options = page.context["js_data"]["creation_options"]
    assert options["publicKey"]["authenticatorSelection"]["residentKey"] == "required"
    assert (
        options["publicKey"]["authenticatorSelection"]["userVerification"] == "required"
    )
    if bad == "challenge":
        options["publicKey"]["challenge"] = websafe_encode(b"wrong challenge")
    credential = Device().register(
        options,
        origin="https://evil.example" if bad == "origin" else ORIGIN,
        verified=bad != "verification",
    )
    response = client.post(
        reverse("mfa_add_webauthn"),
        {
            "challenge_id": page.context["js_data"]["challenge_id"],
            "credential": json.dumps(credential),
            "passwordless": "" if bad == "passwordless" else "True",
        },
        secure=True,
    )
    assert response.status_code == 200
    assert response.context["form"].errors
    assert not Authenticator.objects.exists()


@pytest.mark.parametrize(
    "bad", ["origin", "verification", "signature", "challenge", "user"]
)
def test_invalid_assertion_cannot_sign_in(client, user, bad):
    device = register(client, user)
    client.logout()
    credential, challenge_id = login_credential(
        client,
        user,
        device,
        origin="https://evil.example" if bad == "origin" else ORIGIN,
        verified=bad != "verification",
    )
    if bad == "signature":
        credential["response"]["signature"] = websafe_encode(b"forged signature")
    elif bad == "challenge":
        _, challenge_id = login_credential(client, user, device)  # Mismatched attempt.
    elif bad == "user":
        other = User.objects.create_user(email="other@example.com", username="other")
        credential["response"]["userHandle"] = websafe_encode(str(other.pk).encode())
    response = client.post(
        reverse("mfa_login_webauthn"),
        {"credential": json.dumps(credential), "challenge_id": challenge_id},
        secure=True,
    )
    assert response.url == reverse("accounts:login")
    assert "_auth_user_id" not in client.session


def test_assertion_cannot_be_replayed(client, user):
    device = register(client, user)
    client.logout()
    credential, challenge_id = login_credential(client, user, device)
    credential = json.dumps(credential)
    assert STATE_SESSION_KEY in client.session
    url = reverse("mfa_login_webauthn")
    assert client.post(
        url, {"credential": credential, "challenge_id": challenge_id}, secure=True
    ).url == reverse("accounts:onboarding")
    assert STATE_SESSION_KEY not in client.session
    # Check replay after logout separately from challenge consumption above.
    client.logout()
    assert client.post(
        url, {"credential": credential, "challenge_id": challenge_id}, secure=True
    ).url == reverse("accounts:login")
    assert "_auth_user_id" not in client.session


@pytest.mark.parametrize("attribute", ["is_active", "is_verified"])
def test_disabled_or_unverified_account_cannot_use_saved_passkey(
    client, user, attribute
):
    device = register(client, user)
    setattr(user, attribute, False)
    user.save(update_fields=[attribute])
    client.logout()
    credential, challenge_id = login_credential(client, user, device)
    response = client.post(
        reverse("mfa_login_webauthn"),
        {"credential": json.dumps(credential), "challenge_id": challenge_id},
        secure=True,
    )
    assert response.url == reverse("accounts:login")
    assert "_auth_user_id" not in client.session


def test_other_account_cannot_remove_passkey(client, user):
    register(client, user)
    key = Authenticator.objects.get()
    other = User.objects.create_user(
        email="other@example.com", username="other", is_verified=True
    )
    sign_in(client, other)
    url = reverse("mfa_remove_webauthn", args=[key.pk])
    assert client.get(url, secure=True).status_code == 404
    assert client.post(url, secure=True).status_code == 404
    assert Authenticator.objects.filter(pk=key.pk).exists()


def test_passkey_endpoints_require_csrf_and_obey_rollout_flag(client, user, settings):
    csrf_client = Client(enforce_csrf_checks=True)
    for name in ["mfa_add_webauthn", "mfa_login_webauthn"]:
        assert csrf_client.post(reverse(name), secure=True).status_code == 403
    settings.PASSWORDLESS_AUTH_ENABLED = False
    for name in ["mfa_add_webauthn", "mfa_login_webauthn"]:
        assert client.get(reverse(name), secure=True).status_code == 404


def test_passkey_login_is_rate_limited(client):
    for _ in range(10):
        assert (
            client.post(reverse("mfa_login_webauthn"), secure=True).status_code == 302
        )
    assert client.post(reverse("mfa_login_webauthn"), secure=True).status_code == 429
