import json
import re
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from accounts.tasks import init_user_data_task
from accounts.tokens import password_reset_token_generator
from rivals.models import MiniLeague, Team, UserMiniLeague

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def manager(client, settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.SITE_URL = "https://rivals.example.org"
    user = User.objects.create_user(
        username="manager",
        email="manager@example.org",
        password="Old-password-2049",
        fpl_id="11",
        is_verified=True,
    )
    user.team = Team.objects.create(fpl_team_id="11", team_name="Old team")
    user.save()
    client.force_login(user)
    return user


def profile(entry=22):
    return {
        "id": entry,
        "name": "New team",
        "player_first_name": "New",
        "player_last_name": "Manager",
        "summary_overall_points": 42,
        "summary_overall_rank": 10,
        "leagues": {"classic": [{"id": 88, "name": "New league", "league_type": "x"}]},
    }


def league_page():
    return {
        "league": {"id": 88, "name": "Friends", "scoring": "c"},
        "standings": {"results": []},
    }


@pytest.mark.parametrize("async_mode", [True, False])
def test_switch_is_atomic_even_when_refresh_queue_fails(
    client, manager, settings, async_mode
):
    settings.BACKGROUND_TASK_RUN_ASYNC = async_mode
    old = MiniLeague.objects.create(fpl_league_id="77", name="Old league")
    UserMiniLeague.objects.create(user=manager, mini_league=old, source="fpl")
    with (
        patch(
            "accounts.forms.FplApiService.fetch_team_basic_info", return_value=profile()
        ),
        patch(
            "rivals.services.refresh_service.enqueue_user",
            side_effect=RuntimeError("queue unavailable"),
        ),
    ):
        response = client.post(reverse("accounts:update_fpl_id"), {"fpl_id": 22})
    assert response.url == reverse("accounts:profile")
    manager.refresh_from_db()
    assert manager.fpl_id == "11"
    assert manager.team.fpl_team_id == "11"
    assert manager.fpl_revision == 0
    assert manager.customminileagues.get(mini_league=old).active
    assert not Team.objects.filter(fpl_team_id="22").exists()
    assert not MiniLeague.objects.filter(fpl_league_id="88").exists()


def test_stale_init_cannot_overwrite_new_selection(client, manager):
    with patch(
        "accounts.forms.FplApiService.fetch_team_basic_info", return_value=profile()
    ):
        response = client.post(reverse("accounts:update_fpl_id"), {"fpl_id": 22})
    assert response.url == reverse("rivals:dashboard")
    init_user_data_task.now(manager.pk, json.dumps(profile(11)))
    manager.refresh_from_db()
    assert manager.fpl_id == manager.team.fpl_team_id == "22"
    assert manager.fpl_revision == 1


def test_stale_same_team_payload_is_revoked(manager):
    manager.fpl_revision = 2
    manager.save()
    init_user_data_task.now(manager.pk, json.dumps(profile(11)), 1)
    manager.refresh_from_db()
    assert manager.team.team_name == "Old team"


@pytest.mark.parametrize(
    "data", [None, {}, {"id": 22, "leagues": {}}, {**profile(), "id": 33}]
)
def test_incomplete_or_mismatched_profile_preserves_selection(client, manager, data):
    with patch("accounts.forms.FplApiService.fetch_team_basic_info", return_value=data):
        client.post(reverse("accounts:update_fpl_id"), {"fpl_id": 22})
    manager.refresh_from_db()
    assert manager.fpl_id == manager.team.fpl_team_id == "11"


def test_password_recovery_round_trip(client, manager):
    client.logout()
    response = client.post(reverse("accounts:password_reset"), {"email": manager.email})
    assert response.url == reverse("accounts:login")
    assert len(mail.outbox) == 1
    link = re.search(r"https://rivals.example.org(/\S+)", mail.outbox[0].body).group(1)
    response = client.get(link)
    assert response.status_code == 302  # Django removes the token from the URL.
    confirm = response.url
    assert client.get(confirm).status_code == 200
    response = client.post(
        confirm,
        {"new_password1": "New-password-4567!", "new_password2": "New-password-4567!"},
    )
    assert response.url == reverse("accounts:password_reset_complete")
    assert client.get(response.url).status_code == 200
    manager.refresh_from_db()
    assert manager.check_password("New-password-4567!")
    assert not client.get(link).context["validlink"]


def test_reset_response_does_not_disclose_account(client, manager):
    client.logout()
    known = client.post(
        reverse("accounts:password_reset"), {"email": manager.email}, follow=True
    )
    unknown = client.post(
        reverse("accounts:password_reset"),
        {"email": "unknown@example.org"},
        follow=True,
    )
    assert known.status_code == unknown.status_code == 200
    assert list(map(str, known.context["messages"])) == list(
        map(str, unknown.context["messages"])
    )
    assert len(mail.outbox) == 1


def test_email_failure_keeps_generic_reset_response(client, manager):
    with patch(
        "accounts.forms.PasswordResetForm.save",
        side_effect=RuntimeError("SMTP unavailable"),
    ):
        response = client.post(
            reverse("accounts:password_reset"), {"email": manager.email}
        )
    assert response.url == reverse("accounts:login")


def test_verification_token_cannot_reset_password(client, manager):
    token = default_token_generator.make_token(manager)
    assert not password_reset_token_generator.check_token(manager, token)
    response = client.get(
        reverse(
            "accounts:password_reset_confirm",
            args=[urlsafe_base64_encode(force_bytes(manager.pk)), token],
        )
    )
    assert response.status_code == 200
    assert not response.context["validlink"]


@pytest.mark.parametrize(
    "raw_id", ["", "-1", "0", "garbage", "2147483648", "https://host/88"]
)
def test_invalid_league_ids_never_reach_upstream(client, manager, raw_id):
    with patch(
        "rivals.services.manual_league_service.FplApiService.fetch_mini_league_page"
    ) as fetch:
        response = client.post(
            reverse("rivals:minileagues.store"), {"fpl_league_id": raw_id}
        )
    assert response.url == reverse("rivals:minileagues.create")
    fetch.assert_not_called()
    assert not MiniLeague.objects.exists()


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        {"league": {"name": "Unknown"}},
        {**league_page(), "league": {"id": 99, "name": "Wrong"}},
        {**league_page(), "league": {"id": 88, "name": "H2H", "scoring": "h"}},
    ],
)
def test_failed_league_validation_does_not_leave_phantom(client, manager, data):
    with patch(
        "rivals.services.manual_league_service.FplApiService.fetch_mini_league_page",
        return_value=data,
    ) as fetch:
        for _ in range(2):
            response = client.post(
                reverse("rivals:minileagues.store"), {"fpl_league_id": "88"}
            )
            assert response.url == reverse("rivals:minileagues.create")
    assert fetch.call_count == 2
    assert not MiniLeague.objects.exists()
    assert not manager.customminileagues.exists()


def test_import_queue_failure_rolls_back_all_records(client, manager):
    with (
        patch(
            "rivals.services.manual_league_service.FplApiService.fetch_mini_league_page",
            return_value=league_page(),
        ),
        patch(
            "rivals.views.minileague_views.enqueue_refresh",
            side_effect=RuntimeError("queue failed"),
        ),
    ):
        response = client.post(
            reverse("rivals:minileagues.store"), {"fpl_league_id": "88"}
        )
    assert response.url == reverse("rivals:minileagues.create")
    assert not MiniLeague.objects.exists()
    assert not manager.customminileagues.exists()


def test_valid_manual_league_is_idempotent(client, manager):
    with patch(
        "rivals.services.manual_league_service.FplApiService.fetch_mini_league_page",
        return_value=league_page(),
    ):
        for _ in range(2):
            response = client.post(
                reverse("rivals:minileagues.store"), {"fpl_league_id": "88"}
            )
    membership = manager.customminileagues.get()
    assert response.url == reverse(
        "rivals:minileagues.show", args=[membership.mini_league_id]
    )
    assert membership.source == "manual" and membership.active


def test_league_limit_uses_existing_route_and_preserves_records(client, manager):
    for n in range(10):
        league = MiniLeague.objects.create(fpl_league_id=str(n + 1), name="League")
        UserMiniLeague.objects.create(user=manager, mini_league=league, source="manual")
    with patch(
        "rivals.services.manual_league_service.FplApiService.fetch_mini_league_page",
        return_value=league_page(),
    ):
        response = client.post(
            reverse("rivals:minileagues.store"), {"fpl_league_id": "88"}
        )
    assert response.url == reverse("rivals:minileagues.create")
    assert MiniLeague.objects.count() == 10


@pytest.mark.parametrize(
    "route",
    [
        "accounts:password_reset",
        "rivals:minileagues.index",
        "rivals:minileagues.create",
    ],
)
def test_missing_pages_are_rendered(client, manager, route):
    assert client.get(reverse(route)).status_code == 200


def test_overlapping_update_cannot_replace_later_completed_request(client, manager):
    def newer_switch_completed(_):
        User.objects.filter(pk=manager.pk).update(fpl_revision=1)
        return profile()

    with patch(
        "accounts.forms.FplApiService.fetch_team_basic_info",
        side_effect=newer_switch_completed,
    ):
        response = client.post(reverse("accounts:update_fpl_id"), {"fpl_id": 22})
    manager.refresh_from_db()
    assert response.url == reverse("accounts:profile")
    assert manager.fpl_id == manager.team.fpl_team_id == "11"
    assert manager.fpl_revision == 1


def test_failed_initialization_is_reported_to_worker(manager):
    with pytest.raises(RuntimeError, match="initialization failed"):
        init_user_data_task.now(manager.pk, json.dumps({"id": 11, "leagues": {}}))


def test_expired_reset_token_is_rejected(client, manager, settings):
    from datetime import datetime, timedelta

    settings.PASSWORD_RESET_TIMEOUT = 60
    with patch.object(
        password_reset_token_generator,
        "_now",
        return_value=datetime.now() - timedelta(seconds=120),
    ):
        token = password_reset_token_generator.make_token(manager)
    response = client.get(
        reverse(
            "accounts:password_reset_confirm",
            args=[urlsafe_base64_encode(force_bytes(manager.pk)), token],
        )
    )
    assert not response.context["validlink"]


def test_existing_phantom_record_cannot_skip_validation(client, manager):
    league = MiniLeague.objects.create(fpl_league_id="88", name="Old phantom")
    with patch(
        "rivals.services.manual_league_service.FplApiService.fetch_mini_league_page",
        return_value=None,
    ) as fetch:
        response = client.post(
            reverse("rivals:minileagues.store"), {"fpl_league_id": "88"}
        )
    assert response.url == reverse("rivals:minileagues.create")
    fetch.assert_called_once_with("88", 1)
    assert not manager.customminileagues.exists()
    league.refresh_from_db()
    assert league.name == "Old phantom"
