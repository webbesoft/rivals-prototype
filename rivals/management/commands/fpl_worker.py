import time

from background_task.models import Task
from django.core.management import call_command
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test.utils import override_settings

from rivals.models import SyncState
from rivals.tasks import refresh_tick


class Command(BaseCommand):
    help = "Run recurring FPL refreshes and the existing background/email task worker"

    def add_arguments(self, parser):
        parser.add_argument("--duration", type=int, default=0)
        parser.add_argument("--wait-for-migrations", type=int, default=0)

    def handle(self, *args, **options):
        if not settings.DEBUG and not settings.TESTING:
            call_command("check_production_config")
        # Standalone workers and rolling restarts may encounter pending migrations.
        deadline = time.monotonic() + options["wait_for_migrations"]
        while True:
            executor = MigrationExecutor(connection)
            if not executor.migration_plan(executor.loader.graph.leaf_nodes()):
                break
            if time.monotonic() >= deadline:
                raise CommandError(
                    "Apply database migrations before starting the FPL worker."
                )
            self.stdout.write("Waiting for database migrations…")
            time.sleep(2)
        with transaction.atomic():
            # Include locked dispatchers: rolling restarts must not seed a second loop.
            SyncState.objects.get_or_create(key="worker", defaults={"kind": "worker"})
            if not Task.objects.filter(
                task_name="rivals.tasks.refresh_tick", failed_at__isnull=True
            ).exists():
                refresh_tick(repeat=60, priority=10)
        # One serial consumer suits the shared SQLite volume. Abandoned task locks
        # expire alongside refresh leases, even after a container is killed.
        with override_settings(BACKGROUND_TASK_RUN_ASYNC=False, MAX_RUN_TIME=900):
            call_command("process_tasks", duration=options["duration"], sleep=2)
