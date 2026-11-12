from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError

from rivalspy.security import validate_production_settings


class Command(BaseCommand):
    help = (
        "Check resolved production settings after the runtime environment is injected"
    )

    def handle(self, *args, **options):
        if settings.DEBUG:
            self.stdout.write("Development configuration; production checks skipped.")
            return
        names = (
            "SECRET_KEY",
            "ALLOWED_HOSTS",
            "SITE_URL",
            "DEFAULT_FROM_EMAIL",
            "EMAIL_BACKEND",
            "EMAIL_HOST",
            "EMAIL_USE_TLS",
            "EMAIL_USE_SSL",
        )
        try:
            validate_production_settings(
                {name: getattr(settings, name) for name in names}
            )
        except ImproperlyConfigured as error:
            raise CommandError(str(error)) from None
        self.stdout.write(self.style.SUCCESS("Production configuration is valid."))
