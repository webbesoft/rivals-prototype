import io
import json
import uuid
from unittest.mock import patch

import pytest
import requests
from django.core.management import call_command
from django.utils import timezone

from rivals.models import AIBriefing, AIProviderCall, AIUsage, AIUsageEvent
from rivals.services.ai_briefing_service import (
    complete_briefing,
    request_briefing,
    evidence_for,
)
from rivals.services.ai_credentials import AIUnavailable, disconnect
from rivals.services.ai_usage_audit import prepare_call
from rivals.services.gemini_service import Gemini, TOKEN_RESERVATION
from tests.unit.test_ai_briefings import (
    KEY,
    NOW,
    user,
    credential,
    report,
    isolated,
    generated,
    provider_response,
)


def payload(**changes):
    data = generated()
    data["responseId"] = "response_example-123"
    data["usageMetadata"]["totalTokenCount"] = 850
    data["usageMetadata"]["cachedContentTokenCount"] = 100
    data.update(changes)
    return data


def test_reservations_reuse_and_denials_have_correlated_audit(user, credential, report):
    digest_id = uuid.uuid4()
    run = request_briefing(
        user, report, enqueue=False, trigger="scheduled", digest_id=digest_id
    )
    assert request_briefing(user, report).pk == run.pk
    with pytest.raises(AIUnavailable):
        request_briefing(user, {**report, "fingerprint": "b" * 64})
    assert AIUsage.objects.get(user=user).reserved_tokens == TOKEN_RESERVATION
    assert run.trigger == "scheduled" and run.source_digest_id == digest_id
    reserved = AIUsageEvent.objects.get(event="reserved")
    assert reserved.briefing_id == run.pk and reserved.source_digest_id == digest_id
    assert (
        reserved.trigger == "scheduled"
        and reserved.reserved_tokens == TOKEN_RESERVATION
    )
    assert AIUsageEvent.objects.get(event="reused").briefing_id == run.pk
    assert AIUsageEvent.objects.get(event="denied").reason == "cooldown"


def test_success_records_usage_before_validation_and_no_duplicate_attempt(
    user, credential, report
):
    run = request_briefing(user, report)
    with (
        patch("rivals.services.ai_briefing_service.DecisionEngine") as engine,
        patch(
            "requests.sessions.Session.request",
            return_value=provider_response(payload()),
        ) as http,
    ):
        engine.return_value.report.return_value = report
        complete_briefing(str(run.pk))
        complete_briefing(str(run.pk))
    assert http.call_count == 1
    call = AIProviderCall.objects.get(briefing=run)
    assert call.outcome == "response" and call.http_status == 200
    assert (
        call.input_tokens == 800
        and call.candidate_tokens == 40
        and call.thought_tokens == 10
    )
    assert (
        call.total_tokens == 850 and call.cached_tokens == 100 and call.usage_complete
    )
    assert call.provider_response_id == "response_example-123"
    assert call.duration_ms is not None and call.input_bytes > 0
    events = AIUsageEvent.objects.filter(briefing_id=run.pk)
    assert events.filter(event="provider_started").count() == 1
    assert events.filter(event="provider_response").count() == 1
    assert events.filter(event="duplicate_skipped").count() == 1
    assert not events.filter(event="provider_unknown").exists()


def test_invalid_output_preserves_potentially_billable_usage(user, credential, report):
    run = request_briefing(user, report)
    invalid = payload()
    invalid["candidates"][0]["content"]["parts"][0]["text"] = json.dumps(
        {"priorities": ["invented"]}
    )
    with (
        patch("rivals.services.ai_briefing_service.DecisionEngine") as engine,
        patch(
            "requests.sessions.Session.request", return_value=provider_response(invalid)
        ),
    ):
        engine.return_value.report.return_value = report
        complete_briefing(str(run.pk))
    run.refresh_from_db()
    call = AIProviderCall.objects.get(briefing=run)
    assert run.status == "failed" and call.total_tokens == 850
    assert call.outcome == "response" and call.usage_complete
    assert AIUsageEvent.objects.filter(
        briefing_id=run.pk, event="output_rejected"
    ).exists()


def test_timeout_is_unknown_not_zero_and_keeps_reservation(user, credential, report):
    run = request_briefing(user, report)
    with (
        patch("rivals.services.ai_briefing_service.DecisionEngine") as engine,
        patch(
            "requests.sessions.Session.request", side_effect=requests.Timeout(KEY)
        ) as http,
    ):
        engine.return_value.report.return_value = report
        complete_briefing(str(run.pk))
        complete_briefing(str(run.pk))
    assert http.call_count == 1
    call = AIProviderCall.objects.get(briefing=run)
    assert (
        call.outcome == "unknown"
        and call.total_tokens is None
        and not call.usage_complete
    )
    assert AIUsage.objects.get(user=user).reserved_tokens == TOKEN_RESERVATION
    assert AIUsageEvent.objects.filter(
        call_id=call.pk, event="provider_unknown"
    ).exists()
    assert KEY not in json.dumps(list(AIUsageEvent.objects.values()), default=str)


@pytest.mark.parametrize("status", [400, 401, 429, 500])
def test_http_errors_record_status_without_provider_body(
    user, credential, report, status
):
    run = request_briefing(user, report)
    with (
        patch("rivals.services.ai_briefing_service.DecisionEngine") as engine,
        patch(
            "requests.sessions.Session.request",
            return_value=provider_response({"error": KEY}, status),
        ),
    ):
        engine.return_value.report.return_value = report
        complete_briefing(str(run.pk))
    call = AIProviderCall.objects.get(briefing=run)
    assert call.outcome == "http_error" and call.http_status == status
    assert call.total_tokens is None
    assert KEY not in json.dumps(list(AIProviderCall.objects.values()), default=str)


def test_missing_usage_stays_null_and_bad_metadata_is_allowlisted(
    user, credential, report
):
    run = request_briefing(user, report)
    call = prepare_call(run, TOKEN_RESERVATION)
    data = payload(
        usageMetadata={"promptTokenCount": -2, "totalTokenCount": KEY},
        responseId=KEY + "\n",
        private=KEY,
    )
    with patch(
        "requests.sessions.Session.request", return_value=provider_response(data)
    ):
        with pytest.raises(AIUnavailable):
            Gemini(call_id=call.pk).prioritise(KEY, evidence_for(report), "protect")
    call.refresh_from_db()
    assert (
        call.input_tokens is None
        and call.total_tokens is None
        and not call.usage_complete
    )
    assert call.provider_response_id == ""


def test_audit_write_failure_blocks_paid_request(user, credential, report):
    run = request_briefing(user, report)
    call = prepare_call(run, TOKEN_RESERVATION)
    with (
        patch(
            "rivals.services.ai_usage_audit.AIUsageEvent.objects.create",
            side_effect=RuntimeError("audit failed"),
        ),
        patch("requests.sessions.Session.request") as http,
    ):
        with pytest.raises(AIUnavailable):
            Gemini(call_id=call.pk).prioritise(KEY, evidence_for(report), "protect")
    http.assert_not_called()
    call.refresh_from_db()
    assert call.outcome == "prepared"  # Audit and claim rolled back together.


def test_generation_requires_unique_durable_call_claim(user, credential, report):
    run = request_briefing(user, report)
    call = prepare_call(run, TOKEN_RESERVATION)
    with patch(
        "requests.sessions.Session.request", return_value=provider_response(payload())
    ) as http:
        with pytest.raises(AIUnavailable):
            Gemini().prioritise(KEY, evidence_for(report), "protect")
        Gemini(call_id=call.pk).prioritise(KEY, evidence_for(report), "protect")
        with pytest.raises(AIUnavailable):
            Gemini(call_id=call.pk).prioritise(KEY, evidence_for(report), "protect")
    assert http.call_count == 1


def test_kill_switch_blocks_new_and_already_queued_requests(
    user, credential, report, settings
):
    run = request_briefing(user, report)
    settings.AI_GENERATION_ENABLED = False
    with pytest.raises(AIUnavailable):
        request_briefing(user, report)
    with (
        patch("rivals.services.ai_briefing_service.DecisionEngine") as engine,
        patch("requests.sessions.Session.request") as http,
    ):
        engine.return_value.report.return_value = report
        complete_briefing(str(run.pk))
    http.assert_not_called()
    assert AIUsageEvent.objects.filter(
        user=user, event="denied", reason="disabled"
    ).exists()


def test_usage_overage_suspends_future_generations_until_review(
    user, credential, report
):
    run = request_briefing(user, report)
    call = prepare_call(run, TOKEN_RESERVATION)
    data = payload()
    data["usageMetadata"].update(candidatesTokenCount=1000, totalTokenCount=1810)
    with patch(
        "requests.sessions.Session.request", return_value=provider_response(data)
    ):
        with pytest.raises(AIUnavailable):
            Gemini(call_id=call.pk).prioritise(KEY, evidence_for(report), "protect")
    call.refresh_from_db()
    assert call.usage_exceeds_reservation and call.total_tokens == 1810
    with pytest.raises(AIUnavailable) as error:
        request_briefing(user, report)
    assert error.value.code == "usage_bounds"
    output = io.StringIO()
    call_command(
        "ai_usage_report", user_id=user.pk, acknowledge_call=str(call.pk), stdout=output
    )
    call.refresh_from_db()
    assert call.reviewed_at and call.usage_exceeds_reservation
    assert AIUsageEvent.objects.filter(
        event="safety_reviewed", call_id=call.pk
    ).exists()


def test_revocation_retains_audit_and_provider_usage(user, credential, report):
    run = request_briefing(user, report)
    call = prepare_call(run, TOKEN_RESERVATION)
    with patch(
        "requests.sessions.Session.request", return_value=provider_response(payload())
    ):
        Gemini(call_id=call.pk).prioritise(KEY, evidence_for(report), "protect")
    disconnect(user)
    call.refresh_from_db()
    run.refresh_from_db()
    assert call.total_tokens == 850 and not run.evidence
    assert AIUsageEvent.objects.filter(user=user, event="key_removed").exists()


def test_committed_console_events_are_structured_and_redacted(
    user, credential, report, caplog, django_capture_on_commit_callbacks
):
    with (
        django_capture_on_commit_callbacks(execute=True),
        caplog.at_level("INFO", logger="rivals.ai_usage"),
    ):
        run = request_briefing(user, report)
        call = prepare_call(run, TOKEN_RESERVATION)
        with patch(
            "requests.sessions.Session.request",
            return_value=provider_response(payload(private=KEY)),
        ):
            Gemini(call_id=call.pk).prioritise(KEY, evidence_for(report), "protect")
    assert KEY not in caplog.text and "PRIVATE RIVAL" not in caplog.text
    assert (
        '"event": "provider_response"' in caplog.text
        and '"total_tokens": 850' in caplog.text
    )
    assert str(call.pk) in caplog.text and str(run.pk) in caplog.text


def test_operator_report_separates_reservations_from_observed_usage(
    user, credential, report
):
    run = request_briefing(user, report)
    call = prepare_call(run, TOKEN_RESERVATION)
    with patch("requests.sessions.Session.request", side_effect=requests.Timeout(KEY)):
        with pytest.raises(AIUnavailable):
            Gemini(call_id=call.pk).prioritise(KEY, evidence_for(report), "protect")
    output = io.StringIO()
    call_command("ai_usage_report", user_id=user.pk, days=7, events=True, stdout=output)
    data = json.loads(output.getvalue())
    assert data["summary"]["attempts_without_complete_usage"] == 1
    assert data["summary"]["reported_token_totals"]["total_tokens"] is None
    assert data["daily_reservations"][0]["reserved_tokens"] == TOKEN_RESERVATION
    assert KEY not in output.getvalue() and "PRIVATE RIVAL" not in output.getvalue()
    assert data["events"] and "Google billing remains authoritative" in data["notice"]


def test_response_identifier_cannot_echo_the_api_key_into_logs(
    user, credential, report
):
    run = request_briefing(user, report)
    call = prepare_call(run, TOKEN_RESERVATION)
    with patch(
        "requests.sessions.Session.request",
        return_value=provider_response(payload(responseId=KEY)),
    ):
        Gemini(call_id=call.pk).prioritise(KEY, evidence_for(report), "protect")
    call.refresh_from_db()
    assert call.provider_response_id == ""


def test_digest_denial_audit_survives_fallback(user, report, settings):
    # Use the same user/report fixtures as the real deadline delivery path.
    from rivals.models import DeadlineDigest, DigestSubscription
    from rivals.services.deadline_digest_service import generate_digest

    DigestSubscription.objects.create(
        user=user, enabled=True, consented_at=timezone.now()
    )
    settings.AI_GENERATION_ENABLED = False
    record = DeadlineDigest.objects.create(
        user=user,
        team=user.team,
        season="2026/27",
        gameweek=11,
        deadline=timezone.now() + timezone.timedelta(hours=4),
        scheduled_for=timezone.now(),
        generation_state="queued",
        generation=1,
    )
    with patch(
        "rivals.services.deadline_digest_service.fresh_report",
        return_value={
            **report,
            "season": "2026/27",
            "gameweek": 11,
            "deadline": record.deadline.isoformat(),
        },
    ):
        generate_digest.now(str(record.pk), 1)
    record.refresh_from_db()
    assert record.generation_state == "ready"
    assert AIUsageEvent.objects.filter(
        user=user,
        source_digest_id=record.pk,
        trigger="scheduled",
        event="denied",
        reason="disabled",
    ).exists()


@pytest.mark.parametrize("change", ["inactive", "revoked_key", "expired"])
def test_last_moment_revocation_blocks_paid_submission(
    user, credential, report, change
):
    run = request_briefing(user, report)
    call = prepare_call(run, TOKEN_RESERVATION)
    if change == "inactive":
        user.is_active = False
        user.save(update_fields=["is_active"])
    elif change == "revoked_key":
        disconnect(user)
    else:
        AIBriefing.objects.filter(pk=run.pk).update(expires_at=NOW)
    with patch("requests.sessions.Session.request") as http:
        with pytest.raises(AIUnavailable):
            Gemini(call_id=call.pk).prioritise(KEY, evidence_for(report), "protect")
    http.assert_not_called()
    call.refresh_from_db()
    assert call.outcome == "prepared" and call.started_at is None
