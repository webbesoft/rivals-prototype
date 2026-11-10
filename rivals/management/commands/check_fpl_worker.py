from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from rivals.models import SyncState


class Command(BaseCommand):
    help = "Check the durable FPL worker heartbeat and overdue refresh leases"

    def handle(self, *args, **options):
        heartbeat = SyncState.objects.filter(key="worker").first()
        if (
            not heartbeat
            or not heartbeat.last_success_at
            or (timezone.now() - heartbeat.last_success_at).total_seconds() > 300
        ):
            raise CommandError(
                "No recent FPL worker heartbeat; run manage.py fpl_worker and inspect job logs."
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
