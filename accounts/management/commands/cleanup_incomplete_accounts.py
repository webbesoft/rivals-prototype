from django.core.management.base import BaseCommand, CommandError

from accounts.lifecycle import delete_expired_accounts


class Command(BaseCommand):
    help = "Delete accounts whose seven-day FPL onboarding grace period has expired"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        if options["limit"] < 1:
            raise CommandError("--limit must be positive.")
        count = delete_expired_accounts(
            limit=options["limit"], dry_run=options["dry_run"]
        )
        action = "Would delete" if options["dry_run"] else "Deleted"
        self.stdout.write(f"{action} {count} incomplete account(s).")
