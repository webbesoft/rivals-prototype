import asyncio
from datetime import datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.template import Context, Template
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

from accounts.middleware import timezone_middleware
from accounts.timezones import validate_timezone
from rivals.models import DeadlineDigest
from rivals.templatetags.date_filters import local_datetime

DEADLINE = datetime(2026, 11, 21, 11, tzinfo=dt_timezone.utc)


@pytest.fixture
def account(db, settings):
    settings.PASSWORDLESS_AUTH_ENABLED = False
    return get_user_model().objects.create_user(
        username="timezone-manager",
        email="timezone@example.com",
        password="Timezone-password-123",
        is_verified=True,
    )


@pytest.mark.parametrize(
    "invalid", ["Mars/Olympus", "UTC+2", "../../etc/passwd", "localtime"]
)
def test_timezone_validation_rejects_unknown_or_machine_specific_names(invalid):
    with pytest.raises(ValidationError):
        validate_timezone(invalid)


def test_profile_saves_timezone_and_does_not_detect_again(client, account):
    client.force_login(account)
    response = client.get(reverse("accounts:profile"))
    assert "timezone-default.js" in response.content.decode()
    response = client.post(
        reverse("accounts:update_timezone"), {"timezone": "Africa/Harare"}
    )
    assert response.url == reverse("accounts:profile")
    account.refresh_from_db()
    assert account.timezone == "Africa/Harare"
    response = client.get(response.url)
    assert b'value="Africa/Harare" selected' in response.content
    assert b"timezone-default.js" not in response.content


def test_invalid_timezone_keeps_preference_and_shows_field_error(client, account):
    account.timezone = "Europe/London"
    account.save(update_fields=["timezone"])
    client.force_login(account)
    response = client.post(
        reverse("accounts:update_timezone"), {"timezone": "Mars/Olympus"}
    )
    assert response.status_code == 200
    assert b"Choose a valid timezone" in response.content
    account.refresh_from_db()
    assert account.timezone == "Europe/London"


@pytest.mark.parametrize("saved", ["", "UTC", "America/New_York"])
def test_browser_timezone_is_only_a_default(client, account, saved):
    account.timezone = saved
    account.save(update_fields=["timezone"])
    client.force_login(account)
    response = client.post(
        reverse("accounts:update_timezone"),
        {"timezone": "Asia/Kolkata", "source": "browser"},
    )
    assert response.status_code == 204
    account.refresh_from_db()
    assert account.timezone == (saved or "Asia/Kolkata")


def test_default_can_be_saved_before_fpl_onboarding(client, account, settings):
    settings.PASSWORDLESS_AUTH_ENABLED = True
    client.force_login(account)
    response = client.post(
        reverse("accounts:update_timezone"),
        {"timezone": "Africa/Harare", "source": "browser"},
    )
    assert response.status_code == 204
    account.refresh_from_db()
    assert account.timezone == "Africa/Harare"


def test_timezone_update_requires_login_post_and_csrf(client, account):
    url = reverse("accounts:update_timezone")
    assert client.post(url, {"timezone": "Africa/Harare"}).status_code == 302
    client.force_login(account)
    assert client.get(url).status_code == 405
    protected = Client(enforce_csrf_checks=True)
    protected.force_login(account)
    assert protected.post(url, {"timezone": "Africa/Harare"}).status_code == 403
    page = protected.get(reverse("accounts:profile"))
    assert page.status_code == 200
    assert (
        protected.post(
            url,
            {"timezone": "Africa/Harare", "source": "browser"},
            HTTP_X_CSRFTOKEN=protected.cookies["csrftoken"].value,
        ).status_code
        == 204
    )


def test_invalid_browser_timezone_is_rejected_without_saving(client, account):
    client.force_login(account)
    response = client.post(
        reverse("accounts:update_timezone"),
        {"timezone": "Mars/Olympus", "source": "browser"},
    )
    assert response.status_code == 400
    account.refresh_from_db()
    assert account.timezone == ""


@pytest.mark.parametrize(
    ("zone", "instant", "expected"),
    [
        ("Africa/Harare", DEADLINE, "13:00 CAT"),
        ("Asia/Kolkata", DEADLINE, "16:30 IST"),
        ("America/New_York", DEADLINE, "06:00 EST"),
        ("Europe/London", DEADLINE, "11:00 GMT"),
        ("Europe/London", DEADLINE.replace(month=7), "12:00 BST"),
        ("America/New_York", DEADLINE.replace(month=7), "07:00 EDT"),
        ("Pacific/Auckland", DEADLINE.replace(hour=23), "Sun 22 Nov 12:00 NZDT"),
    ],
)
def test_request_timezone_localizes_template_times_and_resets(zone, instant, expected):
    request = RequestFactory().get("/")
    request.user = SimpleNamespace(timezone=zone)

    def response(_request):
        return HttpResponse(
            Template('{{ instant|date:"D j M H:i T" }}').render(
                Context({"instant": instant})
            )
        )

    with timezone.override("UTC"):
        result = timezone_middleware(response)(request)
        assert expected in result.content.decode()
        assert timezone.get_current_timezone_name() == "UTC"


def test_anonymous_invalid_and_failed_requests_do_not_leak_timezone():
    request = RequestFactory().get("/")

    def response(_request):
        return HttpResponse(timezone.get_current_timezone_name())

    with timezone.override("Europe/Paris"):
        for user in [AnonymousUser(), SimpleNamespace(timezone="Mars/Olympus")]:
            request.user = user
            assert timezone_middleware(response)(request).content == b"UTC"
            assert timezone.get_current_timezone_name() == "Europe/Paris"
        request.user = SimpleNamespace(timezone="Africa/Harare")

        def fail(_request):
            assert timezone.get_current_timezone_name() == "Africa/Harare"
            raise ValueError("Failed view")

        with pytest.raises(ValueError):
            timezone_middleware(fail)(request)
        assert timezone.get_current_timezone_name() == "Europe/Paris"


def test_concurrent_async_requests_keep_separate_timezones():
    async def response(request):
        await asyncio.sleep(0)
        assert timezone.get_current_timezone_name() == request.expected
        return HttpResponse(request.expected)

    async def check():
        middleware = timezone_middleware(response)
        requests = []
        for zone in ["Africa/Harare", "America/New_York"]:

            async def auser(name=zone):
                return SimpleNamespace(timezone=name)

            requests.append(SimpleNamespace(auser=auser, expected=zone))
        with timezone.override("UTC"):
            await asyncio.gather(*(middleware(request) for request in requests))
            assert timezone.get_current_timezone_name() == "UTC"

    asyncio.run(check())


def test_briefing_status_and_email_preview_follow_saved_timezone(client, account):
    from rivals.models import Team

    team = Team.objects.create(fpl_team_id="8765", team_name="Timezone XI")
    account.team = team
    account.timezone = "Africa/Harare"
    account.save(update_fields=["team", "timezone"])
    digest = DeadlineDigest.objects.create(
        user=account,
        team=team,
        season="2026/27",
        gameweek=11,
        deadline=DEADLINE,
        scheduled_for=DEADLINE - timedelta(hours=4),
        generation_state="ready",
        kind="preview",
        report={"gameweek": 11, "baseline": {"lineups": []}, "players": []},
    )
    client.force_login(account)
    url = reverse("accounts:digests.detail", args=[digest.pk])
    status = client.get(url)
    preview = client.get(url, {"email": "1"})
    assert b"13:00 CAT" in status.content
    assert b"13:00 CAT" in preview.content
    assert "11:00 UTC" not in preview.content.decode()
    digest.refresh_from_db()
    assert digest.deadline == DEADLINE


@pytest.mark.parametrize(
    "value",
    [
        "2026-11-21T11:00:00+00:00",
        "2026-11-21 11:00 UTC",
        "2026-11-21 11:00:00",
        DEADLINE,
    ],
)
def test_shared_snapshot_timestamps_are_formatted_only_at_display(value):
    with timezone.override("Africa/Harare"):
        assert local_datetime(value).endswith("13:00 CAT")
    with timezone.override("America/New_York"):
        assert local_datetime(value).endswith("06:00 EST")
