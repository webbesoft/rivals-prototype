from copy import deepcopy
from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from background_task.models import Task
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.template.loader import render_to_string
from django.test import Client
from django.urls import reverse

from rivals.models import (
    AIBriefing,
    AICredential,
    AIUsage,
    DeadlineDigest,
    DigestSubscription,
    SyncState,
)
from rivals.services.deadline_digest_service import (
    dispatch_digests,
    request_digest,
    generate_digest,
    deliver_digest,
    email_context,
    fresh_report,
    unsubscribe_url,
)
from rivals.services.decision_engine import DecisionEngine
from tests.unit.test_decision_engine import NOW, api, manager


@pytest.fixture(autouse=True)
def isolated(settings):
    cache.clear()
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.SITE_URL = "https://rivalspy.example"
    settings.AI_CREDENTIAL_KEYS = ""
    with (
        patch("django.utils.timezone.now", return_value=NOW),
        patch("requests.get", side_effect=AssertionError("Unexpected live HTTP")),
        patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("Unexpected provider HTTP"),
        ),
    ):
        yield
    cache.clear()


@pytest.fixture
def subscriber(manager, api):
    manager.is_verified = True
    manager.save(update_fields=["is_verified"])
    api.fetch_bootstrap_static.return_value["events"][2]["deadline_time"] = (
        NOW + timedelta(hours=4)
    ).isoformat()
    SyncState.objects.create(
        key="context",
        kind="context",
        status="ok",
        last_success_at=NOW,
        payload={"events": api.fetch_bootstrap_static.return_value["events"]},
    )
    DigestSubscription.objects.create(user=manager, enabled=True, consented_at=NOW)
    return manager


@pytest.fixture
def report(subscriber, api):
    return DecisionEngine(subscriber, api=api).report()


def record_for(user, **changes):
    data = dict(
        user=user,
        team=user.team,
        season="2026/27",
        gameweek=11,
        deadline=NOW + timedelta(hours=4),
        scheduled_for=NOW,
    )
    data.update(changes)
    return DeadlineDigest.objects.create(**data)


def test_opt_in_is_off_by_default_and_shared_with_ai_settings(client, manager):
    client.force_login(manager)
    response = client.get(reverse("accounts:gemini"))
    assert response.status_code == 200
    html = response.content.decode()
    assert "Briefing delivery" in html and "Supported provider: Google Gemini" in html
    subscription = DigestSubscription.objects.get(user=manager)
    assert not subscription.enabled and subscription.lead_hours == 4
    assert not subscription.consented_at
    response = client.post(
        reverse("accounts:digests"),
        {"action": "save", "enabled": "on", "lead_hours": 4},
    )
    assert response.status_code == 200
    assert "Verify your email" in response.content.decode()
    subscription.refresh_from_db()
    assert not subscription.enabled


def test_settings_validate_timing_and_pause_revokes_pending(client, subscriber):
    client.force_login(subscriber)
    record = record_for(subscriber)
    url = reverse("accounts:digests")
    assert (
        client.post(
            url, {"action": "save", "enabled": "on", "lead_hours": 7}
        ).status_code
        == 200
    )
    assert (
        client.post(
            url, {"action": "save", "enabled": "on", "paused": "on", "lead_hours": 2}
        ).status_code
        == 302
    )
    saved = DigestSubscription.objects.get(user=subscriber)
    assert saved.paused and saved.lead_hours == 2
    record.refresh_from_db()
    assert record.delivery_state == "skipped"


def test_dispatch_is_deadline_based_and_deduplicates(subscriber):
    dispatch_digests()
    dispatch_digests()
    record = DeadlineDigest.objects.get(user=subscriber)
    assert record.scheduled_for == NOW
    assert record.generation_state == "queued" and record.generation == 1
    tasks = Task.objects.filter(task_name__endswith="generate_digest")
    assert tasks.count() == 1
    assert str(record.pk) in tasks.get().task_params
    assert subscriber.email not in tasks.get().task_params


def test_dispatch_does_not_schedule_unsubscribed_paused_or_unverified(subscriber):
    for changes in ({"enabled": False}, {"enabled": True, "paused": True}):
        DigestSubscription.objects.filter(user=subscriber).update(**changes)
        dispatch_digests()
        assert not DeadlineDigest.objects.exists()
    DigestSubscription.objects.filter(user=subscriber).update(paused=False)
    subscriber.is_verified = False
    subscriber.save()
    dispatch_digests()
    assert not DeadlineDigest.objects.exists()


def test_changed_deadline_reschedules_and_invalidates_old_job(subscriber):
    dispatch_digests()
    record = DeadlineDigest.objects.get()
    old_generation = record.generation
    state = SyncState.objects.get(key="context")
    state.payload["events"][2]["deadline_time"] = (NOW + timedelta(hours=8)).isoformat()
    state.save()
    dispatch_digests()
    record.refresh_from_db()
    assert record.deadline == NOW + timedelta(hours=8)
    assert record.scheduled_for == NOW + timedelta(hours=4)
    assert record.generation > old_generation and record.generation_state == "pending"
    with patch("rivals.services.deadline_digest_service.fresh_report") as fresh:
        generate_digest.now(str(record.pk), old_generation)
    fresh.assert_not_called()


def test_missed_window_and_passed_deadlines_are_skipped(subscriber):
    record = record_for(subscriber, scheduled_for=NOW - timedelta(hours=1))
    DigestSubscription.objects.filter(user=subscriber).update(lead_hours=6)
    dispatch_digests()
    record.refresh_from_db()
    assert record.delivery_state == "skipped" and "missed" in record.notice
    DeadlineDigest.objects.all().delete()
    record = record_for(subscriber, deadline=NOW - timedelta(seconds=1), gameweek=10)
    dispatch_digests()
    record.refresh_from_db()
    assert record.delivery_state == "skipped" and "passed" in record.notice


def test_generation_is_once_and_provider_failure_keeps_structured_report(
    subscriber, report
):
    record = record_for(subscriber, generation_state="queued", generation=1)
    with (
        patch(
            "rivals.services.deadline_digest_service.fresh_report", return_value=report
        ) as fresh,
        patch(
            "rivals.services.deadline_digest_service.request_briefing",
            side_effect=RuntimeError("provider secret"),
        ),
    ):
        generate_digest.now(str(record.pk), 1)
        generate_digest.now(str(record.pk), 1)
    assert fresh.call_count == 1
    record.refresh_from_db()
    assert (
        record.generation_state == "ready"
        and record.report["fingerprint"] == report["fingerprint"]
    )
    context = email_context(record)
    assert context["ai_notice"] and not context["ai_cards"]
    assert "provider secret" not in str(record.report)
    assert Task.objects.filter(task_name__endswith="deliver_digest").exists()


def test_generation_failure_retries_with_a_limit(subscriber):
    dispatch_digests()
    record = DeadlineDigest.objects.get()
    with patch(
        "rivals.services.deadline_digest_service.fresh_report",
        side_effect=RuntimeError("upstream private data"),
    ):
        for attempt in range(1, 4):
            record.refresh_from_db()
            generate_digest.now(str(record.pk), record.generation)
            record.refresh_from_db()
            assert record.attempts == attempt and record.generation_state == "failed"
            assert "private" not in record.notice
            DeadlineDigest.objects.filter(pk=record.pk).update(next_attempt_at=NOW)
            dispatch_digests()
    record.refresh_from_db()
    assert record.attempts == 3 and record.generation_state == "failed"


def test_revocation_and_gameweek_rollover_prevent_generation(subscriber, report):
    record = record_for(subscriber, generation_state="queued", generation=1)
    DigestSubscription.objects.filter(user=subscriber).update(enabled=False)
    with patch("rivals.services.deadline_digest_service.fresh_report") as fresh:
        generate_digest.now(str(record.pk), 1)
    fresh.assert_not_called()
    record.refresh_from_db()
    assert record.generation_state == "skipped"
    DigestSubscription.objects.filter(user=subscriber).update(enabled=True)
    DeadlineDigest.objects.filter(pk=record.pk).update(generation_state="queued")
    changed = {**report, "gameweek": 12}
    with patch(
        "rivals.services.deadline_digest_service.fresh_report", return_value=changed
    ):
        generate_digest.now(str(record.pk), 1)
    record.refresh_from_db()
    assert record.delivery_state == "skipped" and not record.report


def test_multipart_delivery_is_once_and_visually_leads_with_forecast(
    subscriber, report, api
):
    record = record_for(
        subscriber, generation_state="ready", prepared_at=NOW, report=report
    )
    with patch(
        "rivals.services.deadline_digest_service.FplApiService", return_value=api
    ):
        deliver_digest.now(str(record.pk))
        deliver_digest.now(str(record.pk))
    record.refresh_from_db()
    assert record.delivery_state == "sent" and record.recipient == subscriber.email
    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    assert message.to == [subscriber.email] and "AI briefing" in message.subject
    html = message.alternatives[0].content
    assert html.index("Fixtures and playing-time risks") < html.index(
        "Before the deadline"
    )
    assert "Forecast XI" in html and "Bench" in html
    assert "Hold / current-squad baseline" in html
    assert "<script" not in html and "<img" not in html
    assert "FIXTURES AND PLAYING-TIME RISKS" in message.body
    assert "List-Unsubscribe" in message.extra_headers
    assert "Unsubscribe" in html and "AI priorities are unavailable" in html


def test_ambiguous_smtp_failure_never_retries(subscriber, report, api):
    record = record_for(
        subscriber, generation_state="ready", prepared_at=NOW, report=report
    )
    with (
        patch(
            "rivals.services.deadline_digest_service.FplApiService", return_value=api
        ),
        patch(
            "rivals.services.deadline_digest_service.EmailMultiAlternatives.send",
            side_effect=OSError("lost after DATA"),
        ) as send,
    ):
        deliver_digest.now(str(record.pk))
        deliver_digest.now(str(record.pk))
        dispatch_digests()
    assert send.call_count == 1
    record.refresh_from_db()
    assert record.delivery_state == "ambiguous" and "DATA" not in record.notice


def test_safe_connection_failure_retries_before_submission(subscriber, report, api):
    record = record_for(
        subscriber, generation_state="ready", prepared_at=NOW, report=report
    )
    connection = Mock()
    connection.open.side_effect = OSError("connection refused")
    with (
        patch(
            "rivals.services.deadline_digest_service.FplApiService", return_value=api
        ),
        patch(
            "rivals.services.deadline_digest_service.get_connection",
            return_value=connection,
        ),
        patch(
            "rivals.services.deadline_digest_service.EmailMultiAlternatives.send"
        ) as send,
    ):
        deliver_digest.now(str(record.pk))
    send.assert_not_called()
    record.refresh_from_db()
    assert record.delivery_state == "pending" and record.delivery_attempts == 1
    assert record.next_attempt_at > NOW


def test_revocation_during_connection_cancels_before_send(subscriber, report, api):
    record = record_for(
        subscriber, generation_state="ready", prepared_at=NOW, report=report
    )
    connection = Mock()
    connection.open.side_effect = lambda: DigestSubscription.objects.filter(
        user=subscriber
    ).update(enabled=False)
    with (
        patch(
            "rivals.services.deadline_digest_service.FplApiService", return_value=api
        ),
        patch(
            "rivals.services.deadline_digest_service.get_connection",
            return_value=connection,
        ),
        patch(
            "rivals.services.deadline_digest_service.EmailMultiAlternatives.send"
        ) as send,
    ):
        deliver_digest.now(str(record.pk))
    send.assert_not_called()
    record.refresh_from_db()
    assert record.delivery_state == "skipped"


@pytest.mark.parametrize("change", ["deadline", "season", "expired", "team"])
def test_delivery_rechecks_deadline_season_freshness_and_team(
    subscriber, report, api, change
):
    record = record_for(
        subscriber, generation_state="ready", prepared_at=NOW, report=report
    )
    if change == "deadline":
        api.fetch_bootstrap_static.return_value["events"][2]["deadline_time"] = (
            NOW + timedelta(hours=5)
        ).isoformat()
    elif change == "season":
        for event in api.fetch_bootstrap_static.return_value["events"]:
            event["deadline_time"] = event["deadline_time"].replace("2026-", "2027-")
    elif change == "expired":
        record.prepared_at = NOW - timedelta(minutes=11)
        record.save()
    else:
        subscriber.team = None
        subscriber.save()
    with patch(
        "rivals.services.deadline_digest_service.FplApiService", return_value=api
    ):
        deliver_digest.now(str(record.pk))
    record.refresh_from_db()
    assert record.delivery_state == "skipped"
    assert len(getattr(mail, "outbox", [])) == 0


def test_crash_recovery_never_repeats_uncertain_send(subscriber):
    record = record_for(
        subscriber,
        generation_state="ready",
        delivery_state="sending",
        lease_until=NOW - timedelta(seconds=1),
    )
    dispatch_digests()
    record.refresh_from_db()
    assert record.delivery_state == "ambiguous"
    assert not Task.objects.filter(task_name__endswith="deliver_digest").exists()


def test_expired_generation_lease_is_recovered(subscriber):
    record = record_for(
        subscriber,
        generation_state="generating",
        generation=1,
        attempts=1,
        lease_until=NOW - timedelta(seconds=1),
    )
    dispatch_digests()
    record.refresh_from_db()
    assert record.generation_state == "queued" and record.generation == 2


def test_preview_does_not_send_or_enable_subscription(client, subscriber, report):
    DigestSubscription.objects.filter(user=subscriber).update(enabled=False)
    record = request_digest(subscriber, "preview")
    with patch(
        "rivals.services.deadline_digest_service.fresh_report", return_value=report
    ):
        generate_digest.now(str(record.pk), 1)
    record.refresh_from_db()
    assert record.generation_state == "ready"
    assert not DigestSubscription.objects.get(user=subscriber).enabled
    assert not Task.objects.filter(task_name__endswith="deliver_digest").exists()
    client.force_login(subscriber)
    response = client.get(
        reverse("accounts:digests.detail", args=[record.pk]) + "?email=1"
    )
    assert (
        response.status_code == 200
        and b"Fixtures and playing-time risks" in response.content
    )
    other = get_user_model().objects.create_user(
        username="other", email="other@example.com", password="test123456"
    )
    client.force_login(other)
    assert (
        client.get(reverse("accounts:digests.detail", args=[record.pk])).status_code
        == 404
    )


def test_test_email_does_not_suppress_regular_briefing_and_is_rate_limited(subscriber):
    record = request_digest(subscriber, "test")
    assert record.kind == "test"
    dispatch_digests()
    assert DeadlineDigest.objects.filter(kind="scheduled").count() == 1
    with pytest.raises(ValueError, match="wait"):
        request_digest(subscriber, "test")


def test_unsubscribe_requires_post_and_old_token_cannot_revoke_new_consent(
    client, subscriber
):
    url = unsubscribe_url(subscriber).removeprefix("https://rivalspy.example")
    assert client.get(url).status_code == 200
    assert DigestSubscription.objects.get(user=subscriber).enabled
    assert client.post(url).status_code == 200
    assert not DigestSubscription.objects.get(user=subscriber).enabled
    client.force_login(subscriber)
    assert (
        client.post(
            reverse("accounts:digests"),
            {"action": "save", "enabled": "on", "lead_hours": 4},
        ).status_code
        == 302
    )
    assert client.post(url).status_code == 400
    assert DigestSubscription.objects.get(user=subscriber).enabled


def test_settings_and_unsubscribe_enforce_csrf(subscriber):
    client = Client(enforce_csrf_checks=True)
    client.force_login(subscriber)
    assert (
        client.post(reverse("accounts:digests"), {"action": "test"}).status_code == 403
    )
    url = unsubscribe_url(subscriber).removeprefix("https://rivalspy.example")
    assert client.post(url).status_code == 403


def test_fresh_report_bypasses_cached_players_and_stored_squad(subscriber, api):
    api.fetch_team_basic_info.return_value = {
        "name": "Fresh XI",
        "player_first_name": "A",
        "player_last_name": "Manager",
        "summary_overall_points": 100,
        "summary_overall_rank": 1234,
        "summary_event_points": 50,
        "last_deadline_total_transfers": 0,
        "last_deadline_bank": 10,
    }
    api.fetch_team_history.return_value = {"current": [], "past": [], "chips": []}
    api.fetch_team_transfers.return_value = []
    api.fetch_team_picks.return_value = {
        "picks": [{"element": pid} for pid in range(1, 16)],
        "entry_history": {"bank": 10},
    }
    with patch(
        "rivals.services.deadline_digest_service.FplApiService", return_value=api
    ):
        result = fresh_report(subscriber)
    api.fetch_bootstrap_static.assert_called_once_with(force_refresh=True)
    api.fetch_fixtures_data.assert_called_once_with(force_refresh=True)
    api.fetch_team_picks.assert_called_once_with(subscriber.team.fpl_team_id, 10)
    assert len(result["players"]) == 15 and result["gameweek"] == 11


def test_email_preserves_blank_double_and_risk_labels(subscriber, report):
    changed = deepcopy(report)
    changed["players"][0]["schedule"][0]["opponents"] = []
    changed["players"][1]["schedule"][0]["opponents"] = ["AAA (H)", "BBB (A)"]
    changed["players"][1]["risk_label"] = "Minutes risk"
    record = record_for(subscriber, report=changed, prepared_at=NOW)
    context = email_context(record)
    assert context["outlook"][0]["blanks"] == 1
    assert context["outlook"][0]["doubles"] == 1
    html = render_to_string("emails/deadline_digest.html", context)
    assert (
        "Blank" in html
        and "AAA (H)" in html
        and "BBB (A)" in html
        and "Minutes risk" in html
    )
    for player in changed["players"]:
        assert player["name"] in html


@pytest.fixture
def connected(subscriber, settings):
    import json
    from cryptography.fernet import Fernet
    from rivals.services.ai_credentials import connect

    settings.AI_CREDENTIAL_KEYS = json.dumps({"v1": Fernet.generate_key().decode()})
    settings.AI_CREDENTIAL_ACTIVE_KEY = "v1"
    with patch("rivals.services.gemini_service.Gemini.verify"):
        connect(subscriber, "AIza_Digest_Owner_Secret_Key_123456")
    return subscriber


def test_scheduled_email_uses_own_key_and_shared_usage_limits(connected, report, api):
    user = connected
    record = record_for(user, generation_state="queued", generation=1)

    def prioritise(key, evidence, strategy):
        assert key == "AIza_Digest_Owner_Secret_Key_123456"
        return [item["id"] for item in evidence[:3]], 100, 20

    with (
        patch(
            "rivals.services.deadline_digest_service.fresh_report", return_value=report
        ),
        patch(
            "rivals.services.ai_briefing_service.Gemini.prioritise",
            side_effect=prioritise,
        ) as provider,
    ):
        generate_digest.now(str(record.pk), 1)
        generate_digest.now(str(record.pk), 1)
    assert provider.call_count == 1
    record.refresh_from_db()
    assert record.ai_briefing.status == "ready"
    assert AIUsage.objects.get(user=user).generations == 1
    assert not Task.objects.filter(task_name__endswith="prepare_briefing").exists()
    with patch(
        "rivals.services.deadline_digest_service.FplApiService", return_value=api
    ):
        deliver_digest.now(str(record.pk))
    html = mail.outbox[0].alternatives[0].content
    assert "AI selected these priorities" in html
    assert "AIza_Digest" not in html and "AIza_Digest" not in mail.outbox[0].body
    assert record.ai_briefing.evidence[0]["title"] in html


def test_failed_provider_run_is_not_repeated_on_recovered_digest(connected, report):
    record = record_for(connected, generation_state="queued", generation=1)
    with (
        patch(
            "rivals.services.deadline_digest_service.fresh_report", return_value=report
        ),
        patch(
            "rivals.services.ai_briefing_service.Gemini.prioritise",
            side_effect=RuntimeError("provider private key"),
        ) as provider,
    ):
        generate_digest.now(str(record.pk), 1)
        DeadlineDigest.objects.filter(pk=record.pk).update(
            generation_state="queued", generation=2
        )
        generate_digest.now(str(record.pk), 2)
    assert provider.call_count == 1
    record.refresh_from_db()
    assert record.ai_briefing.status == "failed" and record.generation_state == "ready"
    assert "AI priorities are unavailable" in email_context(record)["ai_notice"]
    assert "private key" not in str(record.report) + record.notice


def test_key_removal_during_connection_drops_ai_priorities(connected, report, api):
    from rivals.services.ai_credentials import disconnect

    record = record_for(connected, generation_state="queued", generation=1)
    with (
        patch(
            "rivals.services.deadline_digest_service.fresh_report", return_value=report
        ),
        patch(
            "rivals.services.ai_briefing_service.Gemini.prioritise",
            return_value=(["route", "captain"], 100, 20),
        ),
    ):
        generate_digest.now(str(record.pk), 1)
    backend = mail.get_connection()
    with (
        patch(
            "rivals.services.deadline_digest_service.FplApiService", return_value=api
        ),
        patch(
            "rivals.services.deadline_digest_service.get_connection",
            return_value=backend,
        ),
        patch.object(backend, "open", side_effect=lambda: disconnect(connected)),
    ):
        deliver_digest.now(str(record.pk))
    html = mail.outbox[0].alternatives[0].content
    assert "AI selected these priorities" not in html
    assert "AI priorities are unavailable" in html
    assert "Fixtures and playing-time risks" in html


def test_generation_executes_through_existing_worker(subscriber, report):
    from django.core.management import call_command
    from background_task.models import CompletedTask

    record = request_digest(subscriber, "preview")
    with patch(
        "rivals.services.deadline_digest_service.fresh_report", return_value=report
    ):
        call_command("process_tasks", duration=1, sleep=0.01, verbosity=0)
    record.refresh_from_db()
    assert record.generation_state == "ready"
    completed = CompletedTask.objects.get(task_name__endswith="generate_digest")
    assert not completed.last_error


def test_pause_while_refreshing_does_not_spend_provider_quota(subscriber, report):
    record = record_for(subscriber, generation_state="queued", generation=1)

    def refreshed(user):
        DigestSubscription.objects.filter(user=user).update(paused=True)
        return report

    with (
        patch(
            "rivals.services.deadline_digest_service.fresh_report",
            side_effect=refreshed,
        ),
        patch("rivals.services.deadline_digest_service.request_briefing") as provider,
    ):
        generate_digest.now(str(record.pk), 1)
    provider.assert_not_called()
    record.refresh_from_db()
    assert record.generation_state == "skipped"


def test_old_generation_cannot_skip_new_rescheduled_job(subscriber, report):
    record = record_for(subscriber, generation_state="queued", generation=1)

    def refreshed(user):
        DeadlineDigest.objects.filter(pk=record.pk).update(
            generation=2, generation_state="queued"
        )
        return {**report, "gameweek": 12}

    with patch(
        "rivals.services.deadline_digest_service.fresh_report", side_effect=refreshed
    ):
        generate_digest.now(str(record.pk), 1)
    record.refresh_from_db()
    assert record.generation_state == "queued" and record.generation == 2
