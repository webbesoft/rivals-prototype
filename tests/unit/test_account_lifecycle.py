"""Exercise self-service deletion, token revocation and abandoned onboarding."""

import json
import uuid
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import pytest
from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount
from background_task.models import CompletedTask, Task
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from oauth2_provider.models import AccessToken, Application, Grant, RefreshToken
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.lifecycle import delete_account, delete_expired_accounts
from accounts.models import SignInLink
from accounts.onboarding import save_fpl_setup
from accounts.passwordless import redeem_link
from rivals.models import (
    AIBriefing,
    AICredential,
    AIProviderCall,
    AIUsageEvent,
    DeadlineDigest,
    DigestSubscription,
    MiniLeague,
    Team,
    TrackedTeam,
    TransferPlan,
    UserMiniLeague,
)
from rivals.services.init_user_service import InitUserService
from rivals.tasks import refresh_tick
from rivals_mcp.models import Connection

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture(autouse=True)
def auth_settings(settings):
    settings.PASSWORDLESS_AUTH_ENABLED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


@pytest.fixture
def user():
    return User.objects.create_user(
        username="manager", email="manager@example.com", is_verified=True
    )


def connect_team(user):
    team = Team.objects.create(fpl_team_id="123", team_name="Test XI")
    user.team = team
    user.fpl_id = team.fpl_team_id
    user.save(update_fields=["team", "fpl_id"])
    return team


def snapshot():
    return {
        "id": 123,
        "name": "Test XI",
        "player_first_name": "Test",
        "player_last_name": "Manager",
        "summary_overall_points": 100,
        "summary_overall_rank": 1000,
        "leagues": {"classic": []},
    }


def expire(user):
    User.objects.filter(pk=user.pk).update(
        onboarding_expires_at=timezone.now() - timedelta(seconds=1)
    )
    user.refresh_from_db()


@pytest.mark.parametrize("passwordless", [False, True])
def test_delete_requires_login_confirmation_and_post(
    client, user, settings, passwordless
):
    settings.PASSWORDLESS_AUTH_ENABLED = passwordless
    url = reverse("accounts:delete_account")
    assert client.get(url).status_code == 302
    assert client.post(url, {"confirmation_email": user.email}).status_code == 302
    assert User.objects.filter(pk=user.pk).exists()

    client.force_login(user)
    page = client.get(url)
    assert page.status_code == 200
    assert "no-store" in page["Cache-Control"]
    assert b"This cannot be undone" in page.content
    assert User.objects.filter(pk=user.pk).exists()
    for data in ({}, {"confirmation_email": "someone-else@example.com"}):
        response = client.post(url, data)
        assert response.status_code == 200
        assert response.context["form"].errors
        assert User.objects.filter(pk=user.pk).exists()
    assert client.delete(url).status_code == 405
    response = client.post(url, {"confirmation_email": user.email.upper()})
    assert response.url == reverse("accounts:login")
    assert not User.objects.filter(pk=user.pk).exists()
    assert "_auth_user_id" not in client.session
    assert b"Your account has been deleted." in client.get(response.url).content


def test_delete_requires_csrf(user):
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    url = reverse("accounts:delete_account")
    assert client.post(url, {"confirmation_email": user.email}).status_code == 403
    assert User.objects.filter(pk=user.pk).exists()


def test_deletion_is_discoverable_during_onboarding_and_in_profile(client, user):
    client.force_login(user)
    url = reverse("accounts:delete_account")
    onboarding = client.get(reverse("accounts:onboarding"))
    assert f'href="{url}"'.encode() in onboarding.content
    assert b"automatically deleted after seven days" in onboarding.content
    assert client.get(url).context["cancel_url"] == reverse("accounts:onboarding")
    connect_team(user)
    profile = client.get(reverse("accounts:profile"))
    assert f'href="{url}"'.encode() in profile.content
    assert client.get(url).context["cancel_url"] == reverse("accounts:profile")


def test_deletion_removes_private_data_and_revokes_all_access(client, user):
    now = timezone.now()
    team = connect_team(user)
    other = User.objects.create_user(
        username="other", email="other@example.com", team=team
    )
    league = MiniLeague.objects.create(fpl_league_id="55", name="Friends")
    UserMiniLeague.objects.create(user=user, mini_league=league)
    other_membership = UserMiniLeague.objects.create(user=other, mini_league=league)
    TrackedTeam.objects.create(user=user, team=team)
    plan = TransferPlan.objects.create(user=user, team=team)
    other_plan = TransferPlan.objects.create(user=other, team=team)
    DigestSubscription.objects.create(user=user, enabled=True)
    digest = DeadlineDigest.objects.create(
        user=user,
        team=team,
        season="2026/27",
        gameweek=1,
        deadline=now,
        scheduled_for=now,
        recipient=user.email,
    )
    revision = uuid.uuid4()
    AICredential.objects.create(
        user=user,
        ciphertext="encrypted-private-key",
        key_version="v1",
        revision=revision,
        verified_at=now,
        consented_at=now,
    )
    briefing = AIBriefing.objects.create(
        user=user,
        team=team,
        credential_revision=revision,
        fingerprint="fingerprint",
        model="test-model",
        expires_at=now + timedelta(hours=1),
    )
    audit = AIUsageEvent.objects.create(user=user, event="requested")
    call = AIProviderCall.objects.create(
        user=user,
        briefing=briefing,
        briefing_reference=briefing.pk,
        credential_revision=revision,
        model="test-model",
        trigger="planner",
        reserved_tokens=100,
    )
    EmailAddress.objects.create(
        user=user, email=user.email, verified=True, primary=True
    )
    EmailAddress.objects.create(user=user, email="alias@example.com", verified=True)
    SocialAccount.objects.create(user=user, provider="google", uid="google-user")
    for i, email in enumerate((user.email.upper(), "alias@example.com")):
        SignInLink.objects.create(
            digest=f"challenge-{i}", email=email, expires_at=now + timedelta(hours=1)
        )
    token = Token.objects.create(user=user)
    app = Application.objects.create(
        name="Shared assistant",
        client_type="public",
        authorization_grant_type="authorization-code",
    )
    access = AccessToken.objects.create(
        user=user,
        application=app,
        token="access",
        scope="rivals:read",
        expires=now + timedelta(hours=1),
    )
    RefreshToken.objects.create(
        user=user, application=app, token="refresh", access_token=access
    )
    Grant.objects.create(
        user=user,
        application=app,
        code="code",
        expires=now + timedelta(minutes=2),
        redirect_uri="https://assistant.example/callback",
        scope="rivals:read",
    )
    Connection.objects.create(user=user, application=app, authorized_at=now)
    other_browser = Client()
    other_browser.force_login(user)
    api = APIClient()
    api.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    assert api.get("/api/me/").status_code == 200
    client.force_login(user)
    user_id = user.pk
    assert (
        client.post(
            reverse("accounts:delete_account"), {"confirmation_email": user.email}
        ).status_code
        == 302
    )
    assert not User.objects.filter(pk=user_id).exists()
    for model in (
        TrackedTeam,
        AICredential,
        DigestSubscription,
        SocialAccount,
        EmailAddress,
        Token,
        AccessToken,
        RefreshToken,
        Grant,
        Connection,
    ):
        assert not model.objects.filter(user_id=user_id).exists()
    assert not TransferPlan.objects.filter(pk=plan.pk).exists()
    assert not DeadlineDigest.objects.filter(pk=digest.pk).exists()
    assert not AIBriefing.objects.filter(pk=briefing.pk).exists()
    assert not SignInLink.objects.exists()
    assert redeem_link("challenge-0") is None
    assert api.get("/api/me/").status_code in (401, 403)
    assert other_browser.get(reverse("accounts:profile")).url.startswith(
        reverse("accounts:login")
    )
    assert User.objects.filter(pk=other.pk).exists()
    assert Team.objects.filter(pk=team.pk).exists()
    assert MiniLeague.objects.filter(pk=league.pk).exists()
    assert UserMiniLeague.objects.filter(pk=other_membership.pk).exists()
    assert TransferPlan.objects.filter(pk=other_plan.pk).exists()
    assert Application.objects.filter(pk=app.pk).exists()
    audit.refresh_from_db()
    call.refresh_from_db()
    assert audit.user_id is None
    assert call.user_id is None and call.briefing_id is None


def test_deletion_cleans_legacy_jobs_without_touching_other_accounts(user):
    other = User.objects.create_user(username="other", email="other@example.com")
    for model in (Task, CompletedTask):
        for owner in (user, other):
            for name, args, kwargs in (
                ("init_user_data_task", [owner.pk, json.dumps(snapshot())], {}),
                ("send_verification_email_task", [], {"user_id": owner.pk}),
            ):
                model.objects.create(
                    task_name=f"accounts.tasks.{name}",
                    task_params=json.dumps([args, kwargs]),
                    task_hash=uuid.uuid4().hex,
                    run_at=timezone.now(),
                )
    assert delete_account(user.pk)
    for model in (Task, CompletedTask):
        assert model.objects.count() == 2
        for job in model.objects.all():
            args, kwargs = json.loads(job.task_params)
            assert (args[0] if args else kwargs["user_id"]) == other.pk
    assert not delete_account(user.pk)


def test_grace_period_starts_at_account_creation_and_is_not_extended_by_signin():
    now = timezone.now()
    with patch("django.utils.timezone.now", return_value=now):
        user = User.objects.create_user(username="new", email="new@example.com")
    assert user.onboarding_expires_at == now + timedelta(days=7)
    with patch("django.utils.timezone.now", return_value=now + timedelta(days=6)):
        user.last_login = timezone.now()
        user.save()
    user.refresh_from_db()
    assert user.onboarding_expires_at == now + timedelta(days=7)
    assert (
        delete_expired_accounts(now=now + timedelta(days=7) - timedelta(seconds=1)) == 0
    )
    assert delete_expired_accounts(now=now + timedelta(days=7)) == 1
    assert not User.objects.filter(pk=user.pk).exists()


@pytest.mark.parametrize(
    "protected", ["is_staff", "is_superuser", "completed", "ready"]
)
def test_cleanup_preserves_completed_and_privileged_accounts(user, protected):
    expire(user)
    if protected == "ready":
        connect_team(user)
    elif protected == "completed":
        user.onboarding_expires_at = None
        user.save(update_fields=["onboarding_expires_at"])
    else:
        setattr(user, protected, True)
        user.save(update_fields=[protected])
    assert delete_expired_accounts() == 0
    assert User.objects.filter(pk=user.pk).exists()


@pytest.mark.parametrize("legacy", [False, True])
def test_successful_setup_permanently_cancels_deletion(user, legacy):
    if legacy:
        user.fpl_id = "123"
        user.save(update_fields=["fpl_id"])
        assert InitUserService(user).save_user_info(snapshot())
    else:
        save_fpl_setup(user, 123, snapshot())
    user.refresh_from_db()
    assert user.team_id
    assert user.onboarding_expires_at is None
    user.team = None
    user.save(update_fields=["team"])
    assert delete_expired_accounts(now=timezone.now() + timedelta(days=30)) == 0
    assert User.objects.filter(pk=user.pk).exists()


def test_failed_setup_preserves_original_deadline(user):
    deadline = user.onboarding_expires_at
    with patch.object(InitUserService, "save_user_info", return_value=False):
        with pytest.raises(ValueError):
            save_fpl_setup(user, 123, snapshot())
    user.refresh_from_db()
    assert user.onboarding_expires_at == deadline
    assert user.team_id is None


def test_cleanup_rechecks_deadline_after_candidate_selection(user):
    expire(user)
    from accounts import lifecycle

    original_atomic = lifecycle.transaction.atomic

    def complete_before_lock():
        User.objects.filter(pk=user.pk).update(onboarding_expires_at=None)
        return original_atomic()

    with patch.object(
        lifecycle.transaction, "atomic", side_effect=complete_before_lock
    ):
        assert delete_expired_accounts() == 0
    assert User.objects.filter(pk=user.pk).exists()


def test_cleanup_is_bounded_and_dry_run_has_no_effect(user):
    expire(user)
    other = User.objects.create_user(username="other", email="other@example.com")
    expire(other)
    out = StringIO()
    call_command("cleanup_incomplete_accounts", dry_run=True, stdout=out)
    assert "Would delete 2" in out.getvalue()
    assert User.objects.filter(pk__in=[user.pk, other.pk]).count() == 2
    assert delete_expired_accounts(limit=1) == 1
    assert User.objects.filter(pk__in=[user.pk, other.pk]).count() == 1
    out = StringIO()
    call_command("cleanup_incomplete_accounts", stdout=out)
    assert "Deleted 1" in out.getvalue()
    with pytest.raises(CommandError):
        call_command("cleanup_incomplete_accounts", limit=0)


def test_worker_cleans_expired_accounts_even_when_fpl_is_unavailable(user):
    expire(user)
    with (
        patch(
            "rivals.tasks.GameweekContextService.cached", return_value={"stale": True}
        ),
        patch("rivals.services.refresh_service.enqueue_refresh"),
        patch("rivals.services.deadline_digest_service.dispatch_digests"),
    ):
        refresh_tick.now()
    assert not User.objects.filter(pk=user.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_migration_preserves_existing_fpl_accounts_and_grants_full_grace():
    before = ("accounts", "0008_user_timezone")
    after = ("accounts", "0009_user_onboarding_expires_at")
    executor = MigrationExecutor(connection)
    executor.migrate([before])
    try:
        OldUser = executor.loader.project_state([before]).apps.get_model(
            "accounts", "User"
        )
        pending = OldUser.objects.create(
            username="pending", email="pending@example.com"
        )
        OldUser.objects.filter(pk=pending.pk).update(
            created_at=timezone.now() - timedelta(days=365)
        )
        linked = OldUser.objects.create(
            username="legacy", email="legacy@example.com", fpl_id="123"
        )
        previous = OldUser.objects.create(
            username="previous", email="previous@example.com", fpl_revision=1
        )
        team = Team.objects.create(fpl_team_id="123", team_name="Test XI")
        team_only = OldUser.objects.create(
            username="team-only", email="team-only@example.com", team_id=team.pk
        )
        now = timezone.now()
        executor = MigrationExecutor(connection)
        executor.migrate([after])
        NewUser = executor.loader.project_state([after]).apps.get_model(
            "accounts", "User"
        )
        deadline = NewUser.objects.get(pk=pending.pk).onboarding_expires_at
        assert now + timedelta(days=7) <= deadline <= timezone.now() + timedelta(days=7)
        for user in (linked, previous, team_only):
            assert NewUser.objects.get(pk=user.pk).onboarding_expires_at is None
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes("accounts"))
