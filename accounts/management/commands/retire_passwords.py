from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = (
        "Preview or permanently invalidate legacy passwords, including staff passwords."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        if not settings.PASSWORDLESS_AUTH_ENABLED:
            raise CommandError(
                "Enable PASSWORDLESS_AUTH_ENABLED and verify email/Google sign-in before retiring passwords."
            )
        count = 0
        with transaction.atomic():
            for user in get_user_model().objects.select_for_update().all().iterator():
                if user.has_usable_password():
                    count += 1
                    if options["apply"]:
                        user.set_unusable_password()
                        user.save(update_fields=["password"])
        verb = "Retired" if options["apply"] else "Would retire"
        self.stdout.write(f"{verb} passwords for {count} accounts (including staff).")
