from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.contrib.auth import get_user_model
from django.views.decorators.debug import sensitive_variables

from core.ai_privacy import sensitive_ai
from rivals.models import AICredential
from rivals.services.ai_credentials import decrypt, encrypt, keyring


class Command(BaseCommand):
    help = "Re-encrypt saved Gemini credentials with the configured active key version"

    @sensitive_ai
    @sensitive_variables()
    def handle(self, *args, **options):
        try:
            active, _ = keyring()
            count = 0
            with transaction.atomic():
                for user_id in AICredential.objects.order_by("user_id").values_list(
                    "user_id", flat=True
                ):
                    get_user_model().objects.select_for_update().get(pk=user_id)
                    credential = AICredential.objects.filter(user_id=user_id).first()
                    if credential and credential.key_version != active:
                        version, ciphertext = encrypt(
                            user_id, credential.revision, decrypt(credential)
                        )
                        credential.key_version, credential.ciphertext = (
                            version,
                            ciphertext,
                        )
                        credential.save(update_fields=["key_version", "ciphertext"])
                        count += 1
        except Exception:
            raise CommandError(
                "Rotation failed; no credentials were changed. Check the encryption keyring."
            ) from None
        self.stdout.write(self.style.SUCCESS(f"Rotated {count} credentials."))
