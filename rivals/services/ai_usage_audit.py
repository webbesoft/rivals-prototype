"""Allowlisted audit metadata, stored before provider submission; never raw output."""

import json
import logging
import re
import time

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from rivals.models import AIProviderCall, AIUsageEvent, AICredential, DigestSubscription
from rivals.services.ai_credentials import AIUnavailable

logger = logging.getLogger("rivals.ai_usage")
TRIGGERS = {"planner", "scheduled", "preview", "test"}
EVENTS = {
    "requested",
    "reserved",
    "reused",
    "denied",
    "claimed",
    "duplicate_skipped",
    "cancelled",
    "stale",
    "provider_started",
    "provider_response",
    "provider_unknown",
    "provider_http_error",
    "usage_limit_exceeded",
    "output_rejected",
    "result_ready",
    "result_withheld",
    "failed",
    "connection_requested",
    "connection_denied",
    "key_connected",
    "key_removed",
    "safety_reviewed",
}
REASONS = {
    "",
    "unavailable",
    "daily_limit",
    "cooldown",
    "not_eligible",
    "no_credential",
    "disabled",
    "revoked",
    "expired",
    "plan_changed",
    "transport",
    "invalid_response",
    "invalid_output",
    "usage_bounds",
    "audit_unavailable",
    "http_error",
    "credential_error",
}


def enabled():
    return getattr(settings, "AI_GENERATION_ENABLED", True)


def event(
    code,
    *,
    user_id=None,
    run=None,
    call=None,
    trigger="planner",
    digest_id=None,
    reason="",
    reserved_tokens=None,
):
    # Do not accept arbitrary exception strings or free-form metadata.
    if code not in EVENTS or reason not in REASONS or trigger not in TRIGGERS:
        raise ValueError("Unknown AI audit code")
    record = AIUsageEvent.objects.create(
        event=code,
        user_id=user_id if user_id is not None else getattr(run, "user_id", None),
        briefing_id=getattr(run, "pk", None),
        call_id=getattr(call, "pk", None),
        trigger=trigger,
        source_digest_id=digest_id,
        reason=reason,
        reserved_tokens=reserved_tokens,
    )
    # Emit only committed events, so console and database agree after rollbacks.
    payload = {
        "audit_id": str(record.pk),
        "event": code,
        "user_id": record.user_id,
        "briefing_id": str(record.briefing_id) if record.briefing_id else None,
        "call_id": str(record.call_id) if record.call_id else None,
        "digest_id": str(digest_id) if digest_id else None,
        "trigger": trigger,
        "reason": reason,
        "reserved_tokens": reserved_tokens,
    }
    if call is not None:
        payload["provider"] = {
            field: getattr(call, field)
            for field in (
                "model",
                "outcome",
                "http_status",
                "duration_ms",
                "input_bytes",
                "input_tokens",
                "candidate_tokens",
                "thought_tokens",
                "cached_tokens",
                "total_tokens",
                "usage_complete",
                "usage_exceeds_reservation",
                "provider_response_id",
            )
        }
    emit = (
        logger.warning
        if code in {"usage_limit_exceeded", "provider_unknown"}
        else logger.info
    )
    transaction.on_commit(
        lambda: emit("ai_usage %s", json.dumps(payload, sort_keys=True))
    )
    return record


def run_event(code, run, *, call=None, reason="", reserved_tokens=None):
    return event(
        code,
        run=run,
        call=call,
        trigger=run.trigger,
        digest_id=run.source_digest_id,
        reason=reason,
        reserved_tokens=reserved_tokens,
    )


def prepare_call(run, reservation):
    call, _ = AIProviderCall.objects.get_or_create(
        briefing_reference=run.pk,
        defaults={
            "briefing": run,
            "user_id": run.user_id,
            "credential_revision": run.credential_revision,
            "model": run.model,
            "trigger": run.trigger,
            "source_digest_id": run.source_digest_id,
            "reserved_tokens": reservation,
        },
    )
    return call


def begin_call(call_id, input_bytes):
    if not enabled():
        raise AIUnavailable(
            "AI generation is paused. Your structured advice is still available."
        )
    # A paid request without a durable, unique audit claim is forbidden.
    with transaction.atomic():
        call = AIProviderCall.objects.filter(pk=call_id).first() if call_id else None
        if call is not None:
            run = call.briefing
            if (
                run is None
                or not run.user.is_active
                or not run.user.is_verified
                or run.user.team_id != run.team_id
                or not run.team.active
                or run.expires_at <= timezone.now()
                or not AICredential.objects.filter(
                    user_id=call.user_id, revision=call.credential_revision
                ).exists()
            ):
                raise AIUnavailable(
                    "This AI request is no longer authorised.", code="revoked"
                )
            if (
                call.trigger == "scheduled"
                and not DigestSubscription.objects.filter(
                    user_id=call.user_id,
                    enabled=True,
                    paused=False,
                    consented_at__isnull=False,
                ).exists()
            ):
                raise AIUnavailable(
                    "Scheduled AI briefings are paused.", code="revoked"
                )
        if (
            call
            and AIProviderCall.objects.filter(
                user_id=call.user_id,
                usage_exceeds_reservation=True,
                reviewed_at__isnull=True,
            ).exists()
        ):
            raise AIUnavailable(
                "AI generation is paused pending a usage review.", code="usage_bounds"
            )
        if not call_id or not AIProviderCall.objects.filter(
            pk=call_id, outcome="prepared"
        ).update(
            outcome="dispatching",
            started_at=timezone.now(),
            input_bytes=input_bytes,
        ):
            raise AIUnavailable("This provider request cannot be repeated.")
        call = AIProviderCall.objects.select_related("briefing").get(pk=call_id)
        run_event(
            "provider_started",
            call.briefing,
            call=call,
            reserved_tokens=call.reserved_tokens,
        )
    return time.monotonic()


def finish_call(call_id, started, outcome, *, status=None, result=None):
    if not call_id:
        return
    call = AIProviderCall.objects.select_related("briefing").get(pk=call_id)
    values = {
        "outcome": outcome,
        "completed_at": timezone.now(),
        "duration_ms": min(2**31 - 1, max(0, int((time.monotonic() - started) * 1000))),
        "http_status": status,
    }
    if isinstance(result, dict):
        response_id = result.get("responseId")
        if isinstance(response_id, str) and re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", response_id
        ):
            values["provider_response_id"] = response_id
        usage = result.get("usageMetadata")
        usage = usage if isinstance(usage, dict) else {}
        for target, source in (
            ("input_tokens", "promptTokenCount"),
            ("candidate_tokens", "candidatesTokenCount"),
            ("thought_tokens", "thoughtsTokenCount"),
            ("cached_tokens", "cachedContentTokenCount"),
            ("total_tokens", "totalTokenCount"),
        ):
            count = usage.get(source)
            if type(count) is int and 0 <= count <= 2**63 - 1:
                values[target] = count
        values["usage_complete"] = all(
            key in values
            for key in ("input_tokens", "candidate_tokens", "total_tokens")
        )
        from rivals.services.gemini_service import MAX_INPUT_BYTES, MAX_OUTPUT_TOKENS

        output = values.get("candidate_tokens", 0) + values.get("thought_tokens", 0)
        values["usage_exceeds_reservation"] = (
            values.get("input_tokens", 0) > MAX_INPUT_BYTES
            or output > MAX_OUTPUT_TOKENS
            or values.get("total_tokens", 0) > call.reserved_tokens
        )
    with transaction.atomic():
        if not AIProviderCall.objects.filter(pk=call_id, outcome="dispatching").update(
            **values
        ):
            return
        call.refresh_from_db()
        if call.briefing:
            run_event(
                {
                    "response": "provider_response",
                    "http_error": "provider_http_error",
                    "unknown": "provider_unknown",
                }[outcome],
                call.briefing,
                call=call,
                reason=(
                    ""
                    if outcome == "response"
                    else "http_error" if outcome == "http_error" else "transport"
                ),
            )
            if values.get("usage_exceeds_reservation"):
                run_event(
                    "usage_limit_exceeded",
                    call.briefing,
                    call=call,
                    reason="usage_bounds",
                )
