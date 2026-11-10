"""Read-only by default. Output contains IDs/counters, never credentials or prompts."""

import json
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from rivals.models import AIProviderCall, AIUsage, AIUsageEvent
from rivals.services.ai_usage_audit import event


class Command(BaseCommand):
    help = "Report AI reservations, provider usage and audit events for billing investigations"

    def add_arguments(self, parser):
        parser.add_argument("--user-id", type=int)
        parser.add_argument("--days", type=int, default=7)
        parser.add_argument("--events", action="store_true")
        parser.add_argument(
            "--acknowledge-call",
            type=str,
            help="Acknowledge a reviewed usage overage and release its safety hold; never changes recorded usage",
        )

    def handle(self, *args, **options):
        days = options["days"]
        if not 1 <= days <= 90:
            raise CommandError("Choose between 1 and 90 days.")
        user_id = options["user_id"]
        if options["acknowledge_call"]:
            with transaction.atomic():
                try:
                    call = (
                        AIProviderCall.objects.select_for_update()
                        .select_related("briefing")
                        .get(pk=options["acknowledge_call"])
                    )
                except (AIProviderCall.DoesNotExist, ValueError):
                    raise CommandError("Provider call not found.") from None
                if user_id is not None and call.user_id != user_id:
                    raise CommandError("Provider call does not belong to that user.")
                if not call.usage_exceeds_reservation:
                    raise CommandError("This call has no usage overage to review.")
                if call.reviewed_at is None:
                    call.reviewed_at = timezone.now()
                    call.save(update_fields=["reviewed_at"])
                    event(
                        "safety_reviewed",
                        user_id=call.user_id,
                        run=call.briefing,
                        call=call,
                        trigger=call.trigger,
                        digest_id=call.source_digest_id,
                        reason="usage_bounds",
                    )
        since = timezone.now() - timedelta(days=days)
        calls = AIProviderCall.objects.filter(created_at__gte=since).order_by(
            "created_at", "id"
        )
        events = AIUsageEvent.objects.filter(created_at__gte=since).order_by(
            "created_at", "id"
        )
        reservations = AIUsage.objects.filter(date__gte=since.date()).order_by(
            "date", "user_id"
        )
        holds = AIProviderCall.objects.filter(
            usage_exceeds_reservation=True, reviewed_at__isnull=True
        )
        if user_id is not None:
            calls = calls.filter(user_id=user_id)
            events = events.filter(user_id=user_id)
            reservations = reservations.filter(user_id=user_id)
            holds = holds.filter(user_id=user_id)
        attempted = calls.exclude(outcome="prepared")
        result = {
            "since": since.isoformat(),
            "until": timezone.now().isoformat(),
            "user_id": user_id,
            "notice": "Reservations are safety allowances, not charges. Missing provider usage is unknown, not zero. Google billing remains authoritative; this report does not calculate an invoice.",
            "summary": {
                "provider_attempts": attempted.count(),
                "attempts_without_complete_usage": attempted.filter(
                    usage_complete=False
                ).count(),
                "ambiguous_attempts": attempted.filter(
                    outcome__in=["dispatching", "unknown"]
                ).count(),
                "reported_token_totals": attempted.aggregate(
                    **{
                        field: Sum(field)
                        for field in [
                            "input_tokens",
                            "candidate_tokens",
                            "thought_tokens",
                            "cached_tokens",
                            "total_tokens",
                        ]
                    }
                ),
                "unreviewed_usage_overages": holds.count(),
            },
            "daily_reservations": list(
                reservations.values(
                    "user_id",
                    "date",
                    "generations",
                    "reserved_tokens",
                    "connections",
                    "last_generation_at",
                )
            ),
            "calls": list(
                calls.values(
                    "id",
                    "user_id",
                    "briefing_reference",
                    "source_digest_id",
                    "trigger",
                    "model",
                    "credential_revision",
                    "outcome",
                    "reserved_tokens",
                    "input_bytes",
                    "started_at",
                    "completed_at",
                    "duration_ms",
                    "http_status",
                    "provider_response_id",
                    "input_tokens",
                    "candidate_tokens",
                    "thought_tokens",
                    "cached_tokens",
                    "total_tokens",
                    "usage_complete",
                    "usage_exceeds_reservation",
                    "reviewed_at",
                )
            ),
        }
        if options["events"]:
            result["events"] = list(
                events.values(
                    "id",
                    "created_at",
                    "user_id",
                    "briefing_id",
                    "call_id",
                    "source_digest_id",
                    "trigger",
                    "event",
                    "reason",
                    "reserved_tokens",
                )
            )
        self.stdout.write(json.dumps(result, default=str, sort_keys=True))
