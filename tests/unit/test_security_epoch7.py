from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from rest_framework.authtoken.models import Token

from core.models import RequestLimit
from rivals.models import MiniLeague, Team, TrackedTeam, UserMiniLeague

pytestmark = pytest.mark.django_db
PASSWORD = "Security-epoch-7!"


@pytest.fixture
def manager():
    return get_user_model().objects.create_user(
        username="security@example.com",
        email="security@example.com",
        password=PASSWORD,
        is_verified=True,
    )


@pytest.fixture
def rival():
    return Team.objects.create(fpl_team_id="700", team_name="Rival FC")


def test_unverified_api_login_cannot_issue_token(client, manager):
    manager.is_verified = False
    manager.save(update_fields=["is_verified"])
    response = client.post(
        reverse("api:login"),
        {
            "email": manager.email,
            "password": PASSWORD,
        },
        content_type="application/json",
    )
    assert response.status_code == 403
    assert not Token.objects.filter(user=manager).exists()


@pytest.mark.parametrize("authentication", ["token", "session"])
@pytest.mark.parametrize("route", ["api:me", "api:dashboard", "api:plans_list_create"])
def test_verification_revocation_blocks_existing_credentials(
    client, manager, authentication, route
):
    token = Token.objects.create(user=manager)
    if authentication == "session":
        client.force_login(manager)
    manager.is_verified = False
    manager.save(update_fields=["is_verified"])
    headers = (
        {"HTTP_AUTHORIZATION": f"Token {token.key}"}
        if authentication == "token"
        else {}
    )
    assert client.get(reverse(route), **headers).status_code == 403


def test_verified_api_login_and_access(client, manager):
    response = client.post(
        reverse("api:login"),
        {
            "email": manager.email,
            "password": PASSWORD,
        },
        content_type="application/json",
    )
    assert response.status_code == 200
    assert (
        client.get(
            reverse("api:me"), HTTP_AUTHORIZATION=f"Token {response.json()['token']}"
        ).status_code
        == 200
    )


@pytest.mark.parametrize(
    "next_url",
    [
        "https://attacker.example/phish",
        "//attacker.example/phish",
        "///attacker.example/phish",
        "https://testserver.attacker.example/",
        'https://attacker.example/" onmouseover="alert(1)',
    ],
)
def test_login_rejects_external_next(client, manager, next_url):
    response = client.post(
        reverse("accounts:login"),
        {
            "username": manager.email,
            "password": PASSWORD,
            "next": next_url,
        },
    )
    assert response.url == reverse("rivals:dashboard")


def test_login_preserves_local_next_through_form(client, manager):
    target = reverse("accounts:profile")
    response = client.get(reverse("accounts:login"), {"next": target})
    assert f'name="next" value="{target}"'.encode() in response.content
    response = client.post(
        reverse("accounts:login"),
        {
            "username": manager.email,
            "password": PASSWORD,
            "next": target,
        },
    )
    assert response.url == target


def test_https_login_rejects_http_downgrade(client, manager):
    response = client.post(
        reverse("accounts:login"),
        {
            "username": manager.email,
            "password": PASSWORD,
            "next": "http://testserver/accounts/profile/",
        },
        secure=True,
    )
    assert response.url == reverse("rivals:dashboard")


@pytest.mark.parametrize(
    "route", ["rivals:teams.track", "rivals:teams.untrack", "accounts:logout"]
)
def test_get_cannot_change_tracking_or_session(manager, rival, route):
    client = Client(enforce_csrf_checks=True)
    client.force_login(manager)
    TrackedTeam.objects.create(user=manager, team=rival)
    url = reverse(route, args=[] if route == "accounts:logout" else [rival.pk])
    assert client.get(url).status_code == 405
    assert TrackedTeam.objects.filter(user=manager, team=rival).exists()
    assert "_auth_user_id" in client.session
    assert client.post(url).status_code == 403


def test_post_tracking_works_and_reactivates_existing_record(client, manager, rival):
    client.force_login(manager)
    tracked = TrackedTeam.objects.create(user=manager, team=rival, active=False)
    with patch("rivals.views.team_views.enqueue_team"):
        assert (
            client.post(reverse("rivals:teams.track", args=[rival.pk])).status_code
            == 302
        )
    tracked.refresh_from_db()
    assert tracked.active
    assert TrackedTeam.objects.filter(user=manager, team=rival).count() == 1
    with patch("rivals.views.team_views.enqueue_team"):
        response = client.get(reverse("rivals:teams.show", args=[rival.pk]))
    assert b"Untrack Rival" in response.content
    assert (
        client.post(reverse("rivals:teams.untrack", args=[rival.pk])).status_code == 302
    )
    assert not TrackedTeam.objects.filter(user=manager, team=rival).exists()


@pytest.mark.parametrize("disabled", ["membership", "league"])
def test_inactive_league_denied_consistently(client, manager, disabled):
    league = MiniLeague.objects.create(
        fpl_league_id="701", name="Private league", active=disabled != "league"
    )
    UserMiniLeague.objects.create(
        user=manager, mini_league=league, active=disabled != "membership"
    )
    client.force_login(manager)
    with patch("rivals.views.minileague_views.enqueue_refresh") as enqueue:
        assert (
            client.get(reverse("rivals:minileagues.show", args=[league.pk])).status_code
            == 404
        )
        assert (
            client.post(
                reverse("rivals:minileagues.sync", args=[league.pk])
            ).status_code
            == 404
        )
        enqueue.assert_not_called()
    assert client.get(reverse("api:league_detail", args=[league.pk])).status_code == 403
    assert client.post(reverse("api:league_sync", args=[league.pk])).status_code == 403
    assert client.get(reverse("api:leagues")).json() == []


def test_login_limit_shared_by_browser_api_and_process_cache(client, manager):
    for _ in range(5):
        assert (
            client.post(
                reverse("accounts:login"),
                {
                    "username": manager.email,
                    "password": "wrong",
                },
            ).status_code
            == 200
        )
    cache.clear()
    for _ in range(5):
        assert (
            Client()
            .post(
                reverse("api:login"),
                {
                    "email": manager.email,
                    "password": "wrong",
                },
                content_type="application/json",
            )
            .status_code
            == 401
        )
    response = Client().post(
        reverse("api:login"),
        {
            "email": manager.email.upper(),
            "password": PASSWORD,
        },
        content_type="application/json",
        HTTP_X_FORWARDED_FOR="203.0.113.99",
    )
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0
    assert not Token.objects.filter(user=manager).exists()
    assert all(
        manager.email not in counter.key for counter in RequestLimit.objects.all()
    )


def test_ip_limit_cannot_be_bypassed_with_forwarded_headers(client):
    from core.rate_limits import POLICIES

    with patch.dict(POLICIES, login=(("ip", 1, 60),)):
        assert (
            client.post(
                reverse("api:login"), {}, content_type="application/json"
            ).status_code
            == 400
        )
        response = client.post(
            reverse("api:login"),
            {},
            content_type="application/json",
            HTTP_X_FORWARDED_FOR="203.0.113.99",
        )
        assert response.status_code == 429


def test_signup_limit_shared_across_entry_points(client):
    with patch("accounts.forms.FplApiService.fetch_team_basic_info") as upstream:
        for _ in range(5):
            assert client.post(reverse("accounts:signup"), {}).status_code == 200
        assert (
            client.post(
                reverse("api:signup"), {}, content_type="application/json"
            ).status_code
            == 429
        )
        upstream.assert_not_called()


def test_resend_cooldown_and_expiry(client, manager):
    manager.is_verified = False
    manager.save(update_fields=["is_verified"])
    session = client.session
    session["pending_verification_email"] = manager.email
    session.save()
    now = timezone.now()
    with (
        patch("accounts.views.send_verification_email_task") as task,
        patch("core.rate_limits.timezone.now", return_value=now),
    ):
        assert client.post(reverse("accounts:resend_verification")).status_code == 302
        response = client.post(reverse("accounts:resend_verification"))
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "60"
        assert task.now.call_count == 1
        with patch(
            "core.rate_limits.timezone.now", return_value=now + timedelta(seconds=61)
        ):
            assert (
                client.post(reverse("accounts:resend_verification")).status_code == 302
            )
        assert task.now.call_count == 2


def test_refresh_limit_shared_across_web_api_and_resources(client, manager, rival):
    from core.rate_limits import POLICIES

    client.force_login(manager)
    TrackedTeam.objects.create(user=manager, team=rival)
    with (
        patch.dict(POLICIES, refresh=(("user", 1, 60),)),
        patch("rivals.views.team_views.enqueue_team") as enqueue,
    ):
        assert (
            client.post(reverse("rivals:teams.sync", args=[rival.pk])).status_code
            == 302
        )
        assert client.post(reverse("api:team_sync", args=[rival.pk])).status_code == 429
        assert client.post(reverse("rivals:rivals.sync")).status_code == 429
        assert enqueue.call_count == 1


def test_tracking_limit_is_idempotent_and_does_not_count_inactive(
    client, manager, rival
):
    client.force_login(manager)
    for index in range(10):
        team = Team.objects.create(fpl_team_id=str(800 + index), team_name="Tracked")
        TrackedTeam.objects.create(user=manager, team=team)
    with patch("rivals.views.team_views.enqueue_team") as enqueue:
        response = client.post(reverse("rivals:teams.track", args=[rival.pk]))
        assert response.status_code == 302
        assert not TrackedTeam.objects.filter(user=manager, team=rival).exists()
        enqueue.assert_not_called()
    tracked = manager.tracked_teams.first()
    with patch("rivals.services.refresh_service.enqueue_team"):
        assert (
            client.post(reverse("api:team_track", args=[tracked.team_id])).status_code
            == 200
        )
        assert (
            client.post(reverse("api:team_track", args=[rival.pk])).status_code == 403
        )
        tracked.active = False
        tracked.save(update_fields=["active"])
        assert (
            client.post(reverse("api:team_track", args=[rival.pk])).status_code == 200
        )


def test_league_store_requires_post(client, manager):
    client.force_login(manager)
    assert client.get(reverse("rivals:minileagues.store")).status_code == 405


@pytest.mark.parametrize(
    "route,args",
    [
        ("rivals:teams.compare.quick", [700]),
        ("rivals:teams.lineup", [700]),
        ("rivals:decisions.workspace", []),
        ("api:suggestions", []),
        ("api:projections", [700]),
    ],
)
def test_expensive_gets_are_limited_before_computation(client, manager, route, args):
    from core.rate_limits import POLICIES, reserve_request
    from django.test import RequestFactory

    client.force_login(manager)
    request = RequestFactory().get("/")
    request.user = manager
    with patch.dict(POLICIES, analysis=(("user", 1, 60),)):
        assert reserve_request(request, "analysis") == 0
        with patch(
            "requests.get", side_effect=AssertionError("Blocked before upstream HTTP")
        ):
            response = client.get(reverse(route, args=args))
        assert response.status_code == 429
        assert int(response.headers["Retry-After"]) > 0
