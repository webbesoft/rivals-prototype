"""Abuse limits shared by browser/API requests and independent of process caches."""

import math
from datetime import timedelta
from functools import wraps

from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from django.utils.crypto import salted_hmac

from core.models import RequestLimit

# (subject, maximum attempts, window seconds). IP limits use REMOTE_ADDR only;
# forwarded headers must be validated by the trusted application server/proxy.
POLICIES = {
    "login": (("ip", 60, 300), ("identity", 10, 300)),
    "signup": (("ip", 5, 3600),),
    "resend": (("ip", 20, 3600), ("identity", 1, 60), ("identity", 5, 3600)),
    "password_reset": (("ip", 10, 3600), ("identity", 3, 3600)),
    "refresh": (("user", 10, 60),),
    "analysis": (("user", 30, 60),),
    "profile_update": (("user", 5, 300),),
    "league_add": (("user", 10, 3600),),
}


def reserve_request(request, action, *, identity=""):
    """Reserve every applicable bucket atomically; return seconds until retry."""
    now = timezone.now()
    subjects = {
        "ip": request.META.get("REMOTE_ADDR", "unknown"),
        "identity": str(identity).strip().casefold(),
        "user": str(request.user.pk) if request.user.is_authenticated else "",
    }
    with transaction.atomic():
        # Bounded cleanup prevents indefinite retention of hashed subjects.
        expired = list(
            RequestLimit.objects.filter(expires_at__lte=now)
            .order_by("expires_at")
            .values_list("pk", flat=True)[:100]
        )
        RequestLimit.objects.filter(pk__in=expired).delete()
        counters = []
        for subject, maximum, seconds in POLICIES[action]:
            value = subjects[subject]
            if not value:
                continue
            key = salted_hmac(
                "request-limit",
                f"{action}:{subject}:{seconds}:{value}",
                algorithm="sha256",
            ).hexdigest()
            counter, _ = RequestLimit.objects.get_or_create(
                key=key, defaults={"expires_at": now + timedelta(seconds=seconds)}
            )
            counter = RequestLimit.objects.select_for_update().get(pk=counter.pk)
            if counter.expires_at <= now:
                counter.attempts = 0
                counter.expires_at = now + timedelta(seconds=seconds)
            if subject == "ip":
                retry = (
                    math.ceil((counter.expires_at - now).total_seconds())
                    if counter.attempts >= maximum
                    else 0
                )
                counter.attempts = min(counter.attempts + 1, maximum)
                counter.save(update_fields=["attempts", "expires_at"])
                if retry:
                    return max(0, retry)
            else:
                counters.append((counter, maximum))
        retry = max(
            (
                math.ceil((counter.expires_at - now).total_seconds())
                for counter, maximum in counters
                if counter.attempts >= maximum
            ),
            default=0,
        )
        # Count attempted identities against the IP limit even when that identity
        # is already blocked, so rotating blocked addresses cannot bypass it.
        for counter, maximum in counters:
            counter.attempts = min(counter.attempts + 1, maximum)
            counter.save(update_fields=["attempts", "expires_at"])
        return max(0, retry)


def rate_limit(action, *, identity_field=None, session_field=None, methods=("POST",)):
    def decorate(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.method in methods:
                identity = (
                    request.POST.get(identity_field, "")
                    if identity_field
                    else request.session.get(session_field, "") if session_field else ""
                )
                retry = reserve_request(request, action, identity=identity)
                if retry:
                    response = HttpResponse(
                        "Too many requests. Please try again later.",
                        status=429,
                        content_type="text/plain",
                    )
                    response["Retry-After"] = str(retry)
                    response["Cache-Control"] = "no-store"
                    return response
            return view(request, *args, **kwargs)

        return wrapped

    return decorate
