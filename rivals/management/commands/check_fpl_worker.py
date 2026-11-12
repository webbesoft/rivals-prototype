import time
import socket

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from rivals.models import SyncState


class Command(BaseCommand):
    help = "Check the durable FPL worker heartbeat and overdue refresh leases"

    def add_arguments(self, parser):
        parser.add_argument(
            "--wait",
            type=int,
            default=0,
            help="Wait up to this many seconds for a healthy worker",
        )

        parser.add_argument(
            "--local-progress",
            action="store_true",
            help="Require a new tick from this container after the check starts",
        )

    def handle(self, *args, **options):
        self.require_local_progress = options["local_progress"]
        self.started_at = timezone.now()
        if options["wait"] < 0:
            raise CommandError("--wait must be non-negative.")
        deadline = time.monotonic() + options["wait"]
        while True:
            try:
                self.check_worker()
                return
            except CommandError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(2, remaining))

    def check_worker(self):
        heartbeat = SyncState.objects.filter(key="worker").first()
        if (
            not heartbeat
            or not heartbeat.last_success_at
            or (timezone.now() - heartbeat.last_success_at).total_seconds() > 300
        ):
            raise CommandError(
                "No recent FPL worker heartbeat; run manage.py fpl_worker and inspect job logs."
            )
        if getattr(self, "require_local_progress", False) and (
            heartbeat.payload.get("worker_id") != socket.gethostname()
            or heartbeat.last_success_at <= self.started_at
        ):
            raise CommandError(
                "Waiting for new task-processing progress from the deployed worker."
            )
        overdue = SyncState.objects.filter(
            status__in=["queued", "running"], lease_until__lt=timezone.now()
        ).count()
        if overdue:
            raise CommandError(
                f"{overdue} refresh leases are overdue; inspect the worker backlog."
            )
        failures = SyncState.objects.filter(status="error").count()
        self.stdout.write(
            self.style.SUCCESS(
                f"FPL worker heartbeat is current; {failures} refreshes are retrying."
            )
        )
