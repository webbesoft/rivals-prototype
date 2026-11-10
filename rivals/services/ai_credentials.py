"""Versioned, authenticated encryption. The keyring is supplied by the operator."""

import json
import re
import uuid

from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from core.ai_privacy import sensitive_ai
from rivals.models import AIBriefing, AICredential, AIUsage


class AIUnavailable(Exception):
    """Only fixed, non-sensitive messages may be used as exception arguments."""


@sensitive_variables()
def keyring():
    try:
        keys = json.loads(settings.AI_CREDENTIAL_KEYS)
        active = settings.AI_CREDENTIAL_ACTIVE_KEY
        if not isinstance(keys, dict) or not keys or active not in keys:
            raise ValueError
        if any(
            not isinstance(k, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", k)
            for k in keys
        ):
            raise ValueError
        if settings.SECRET_KEY in keys.values():
            raise ValueError
        return active, {
            version: Fernet(value.encode()) for version, value in keys.items()
        }
    except Exception:
        raise AIUnavailable("AI briefings are temporarily unavailable.") from None


def configured():
    try:
        keyring()
        return True
    except AIUnavailable:
        return False


@sensitive_variables()
def encrypt(user_id, revision, api_key):
    version, keys = keyring()
    # Bind ciphertext to its owner and revision: copied rows cannot swap credentials.
    payload = json.dumps({"user": user_id, "revision": str(revision), "key": api_key})
    return version, keys[version].encrypt(payload.encode()).decode()


@sensitive_variables()
def decrypt(credential):
    try:
        _, keys = keyring()
        payload = json.loads(
            keys[credential.key_version].decrypt(credential.ciphertext.encode())
        )
        if payload["user"] != credential.user_id or payload["revision"] != str(
            credential.revision
        ):
            raise ValueError
        return payload["key"]
    except Exception:
        raise AIUnavailable("Update your API key to prepare an AI briefing.") from None


def reserve_connection(user):
    with transaction.atomic():
        usage, _ = AIUsage.objects.get_or_create(user=user, date=timezone.now().date())
        usage = AIUsage.objects.select_for_update().get(pk=usage.pk)
        if usage.connections >= 10 or (
            usage.last_connection_at
            and timezone.now() - usage.last_connection_at
            < timezone.timedelta(seconds=30)
        ):
            raise AIUnavailable(
                "Please wait before trying to connect AI briefings again."
            )
        usage.connections += 1
        usage.last_connection_at = timezone.now()
        usage.save(update_fields=["connections", "last_connection_at"])


@sensitive_ai
@sensitive_variables()
def connect(user, api_key):
    from rivals.services.gemini_service import Gemini

    if not user.is_verified:
        raise AIUnavailable("Verify your email before enabling AI briefings.")
    keyring()
    reserve_connection(user)
    previous_revision = (
        AICredential.objects.filter(user=user)
        .values_list("revision", flat=True)
        .first()
    )
    # Validation happens before replacing a working connection.
    Gemini().verify(api_key)
    revision = uuid.uuid4()
    version, ciphertext = encrypt(user.pk, revision, api_key)
    with transaction.atomic():
        # Serialize replacement/deletion, including the first connection.
        current_user = get_user_model().objects.select_for_update().get(pk=user.pk)
        current_revision = (
            AICredential.objects.filter(user=user)
            .values_list("revision", flat=True)
            .first()
        )
        if not current_user.is_verified or current_revision != previous_revision:
            raise AIUnavailable(
                "Your AI briefing connection changed. Please try again."
            )
        AICredential.objects.update_or_create(
            user=user,
            defaults={
                "ciphertext": ciphertext,
                "key_version": version,
                "revision": revision,
                "verified_at": timezone.now(),
                "consented_at": timezone.now(),
            },
        )
        AIBriefing.objects.filter(user=user).update(
            status="cancelled", evidence=[], priorities=[]
        )


@sensitive_ai
def disconnect(user):
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=user.pk)
        AICredential.objects.filter(user=user).delete()
        AIBriefing.objects.filter(user=user).update(
            status="cancelled", evidence=[], priorities=[]
        )
