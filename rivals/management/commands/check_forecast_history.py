import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from rivals.models import FplSnapshot, SyncState
from rivals.services.fpl_api_service import FplApiService
from rivals.services.fpl_snapshot_service import publish_snapshots
from rivals.services.forecast_history_service import (
    history_readiness,
    read_histories,
    refresh_histories,
)
from rivals.services.gameweek_context_service import GameweekContextService
from rivals.services.gameweek_forecast_service import prepare_forecast, publish_forecast
from rivals.tasks import check_refresh_owner


def published_sources():
    # One publication read, with no network or queue side effects on a miss.
    now = timezone.now()
    pair = dict(
        FplSnapshot.objects.filter(
            key__in=("bootstrap", "fixtures"),
            expires_at__gt=now,
            updated_at__lte=now,
        ).values_list("key", "payload")
    )
    return pair.get("bootstrap"), pair.get("fixtures")


class Command(BaseCommand):
    help = "Inspect candidate history coverage; optionally warm bounded batches and capture this deadline's shadow forecasts"

    def add_arguments(self, parser):
        parser.add_argument("--warm-batches", type=int, default=0)
        parser.add_argument("--capture", action="store_true")

    def handle(self, *args, **options):
        batches = options["warm_batches"]
        if not 0 <= batches <= 6:
            raise CommandError(
                "Choose between zero and six batches of at most 20 requests"
            )
        bootstrap, fixtures = published_sources()
        if not bootstrap or fixtures is None:
            raise CommandError(
                "Fresh published bootstrap and fixtures are required; run the FPL worker first"
            )
        context = GameweekContextService.derive(bootstrap.get("events", []))
        if not context["season"] or not context["decision_gameweek"]:
            raise CommandError("No upcoming deadline is available")
        writing = bool(batches or options["capture"])
        state = None
        refreshed, captured = 0, False
        if writing:
            with transaction.atomic():
                state, _ = SyncState.objects.select_for_update().get_or_create(
                    key="manual:forecast-history-warmup",
                    defaults={"kind": "forecast_histories"},
                )
                if state.lease_until and state.lease_until > timezone.now():
                    raise CommandError(
                        "A bounded manual history warmup is already running"
                    )
                SyncState.objects.filter(pk=state.pk).update(
                    generation=F("generation") + 1,
                    status="running",
                    last_attempt_at=timezone.now(),
                    lease_until=timezone.now() + timezone.timedelta(minutes=15),
                )
                state.refresh_from_db()

        def capture():
            # No outer transaction: the forecast publisher owns its deadline checks.
            fresh_bootstrap, fresh_fixtures = published_sources()
            if not fresh_bootstrap or fresh_fixtures is None:
                raise RuntimeError("Published forecast inputs expired during warmup")
            fresh_context = GameweekContextService.derive(
                fresh_bootstrap.get("events", [])
            )
            if any(
                fresh_context[key] != context[key]
                for key in ("season", "decision_gameweek")
            ):
                raise RuntimeError("Forecast season or deadline changed during warmup")
            prepared = prepare_forecast(
                fresh_bootstrap,
                fresh_fixtures,
                histories=read_histories(context["season"]),
            )
            return publish_forecast(
                prepared, publication_guard=lambda: check_refresh_owner(state)
            )

        try:
            # First establish v4 rankings so history warmup also sees its candidates.
            if options["capture"]:
                captured = capture() or captured
            api = FplApiService()
            api.TIMEOUT = 5
            for _ in range(batches):
                snapshots = refresh_histories(
                    api, context, publication_guard=lambda: check_refresh_owner(state)
                )
                with transaction.atomic():
                    check_refresh_owner(state)
                    publish_snapshots(snapshots)
                refreshed += len(snapshots)
                if options["capture"]:
                    captured = capture() or captured
                self.stderr.write(f"Published {len(snapshots)} player histories")
            if writing:
                SyncState.objects.filter(
                    pk=state.pk, generation=state.generation, status="running"
                ).update(
                    status="ok",
                    failures=0,
                    error="",
                    next_attempt_at=None,
                    last_success_at=timezone.now(),
                    lease_until=None,
                )
        except Exception as error:
            if writing:
                SyncState.objects.filter(
                    pk=state.pk, generation=state.generation
                ).update(
                    status="error",
                    failures=F("failures") + 1,
                    error="Manual forecast warmup failed; prior snapshots retained.",
                    lease_until=None,
                )
            raise CommandError(str(error)) from error
        self.stdout.write(
            json.dumps(
                {
                    **history_readiness(context),
                    "refreshed_histories": refreshed,
                    "forecast_captured": captured,
                },
                indent=2,
            )
        )
