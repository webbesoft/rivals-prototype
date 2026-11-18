"""Session-bound WebAuthn challenges, atomically claimed before verification."""

from contextlib import contextmanager
from datetime import timedelta

from allauth.core import context
from allauth.mfa.webauthn.internal.auth import STATE_SESSION_KEY
from django import forms
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac

from .models import PasskeyChallenge


def _session_digest(request):
    if not request.session.session_key:
        request.session.save()
    return salted_hmac(
        "passkey-session", request.session.session_key, algorithm="sha256"
    ).hexdigest()


def issue_challenge(request, purpose, user=None):
    now = timezone.now()
    expired = list(
        PasskeyChallenge.objects.filter(expires_at__lte=now).values_list(
            "pk", flat=True
        )[:100]
    )
    PasskeyChallenge.objects.filter(pk__in=expired).delete()
    return PasskeyChallenge.objects.create(
        session_digest=_session_digest(request),
        user=user,
        purpose=purpose,
        state=request.session[STATE_SESSION_KEY],
        expires_at=now + timedelta(seconds=settings.PASSKEY_CHALLENGE_TTL_SECONDS),
    )


def consume_challenge(request, challenge_id, purpose, user=None):
    with transaction.atomic():
        now = timezone.now()
        candidates = PasskeyChallenge.objects.filter(
            pk=challenge_id,
            session_digest=_session_digest(request),
            user=user,
            purpose=purpose,
            expires_at__gt=now,
            used_at__isnull=True,
        )
        challenge = candidates.first()
        if challenge and candidates.update(used_at=now) == 1:
            return challenge.state
    return None


@contextmanager
def claimed_challenge(challenge_id, purpose, user=None):
    request = context.request
    state = consume_challenge(request, challenge_id, purpose, user)
    if state is None:
        raise forms.ValidationError(
            "This passkey request expired or was already used. Please try again."
        )
    request.session[STATE_SESSION_KEY] = state
    try:
        yield
    finally:
        # Verification failures also consume the attempt. Allauth clears this
        # on success; the database claim protects stale and concurrent sessions.
        request.session.pop(STATE_SESSION_KEY, None)
