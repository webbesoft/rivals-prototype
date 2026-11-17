"""Issue and atomically redeem short-lived email sign-in challenges."""

import hashlib
import secrets
import uuid
from datetime import timedelta

from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from .models import SignInLink
from .onboarding import has_fpl_setup

BACKEND = "accounts.backends.AccountBackend"
SENT_MESSAGE = "Check your inbox for a sign-in link. If it doesn't arrive, wait a minute and try again."


def safe_next(request, value):
    if (
        isinstance(value, str)
        and value
        and len(value) <= 2048
        and url_has_allowed_host_and_scheme(
            value, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        )
    ):
        return value
    return reverse("rivals:dashboard")


def token_digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def issue_link(email, redirect_to):
    email = email.strip().lower()
    user = get_user_model().objects.filter(email__iexact=email).first()
    if user and not user.is_active:
        return
    token = secrets.token_urlsafe(32)
    now = timezone.now()
    # Bounded housekeeping, independent of the cache/process serving the request.
    expired = list(
        SignInLink.objects.filter(expires_at__lt=now).values_list("pk", flat=True)[:100]
    )
    SignInLink.objects.filter(pk__in=expired).delete()
    link = SignInLink.objects.create(
        email=email,
        digest=token_digest(token),
        redirect_to=redirect_to,
        expires_at=now + timedelta(seconds=settings.MAGIC_LINK_TTL_SECONDS),
    )
    url = settings.SITE_URL.rstrip("/") + reverse("accounts:magic_link", args=[token])
    try:
        send_mail(
            "Your RivalSpy sign-in link",
            f"Continue to RivalSpy:\n\n{url}\n\n"
            "This link expires in 10 minutes and can be used once. "
            "Confirm sign-in on the page that opens. If you didn't request it, ignore this email.",
            settings.DEFAULT_FROM_EMAIL,
            [email],
            fail_silently=False,
            html_message=render_to_string(
                "accounts/emails/sign_in.html", {"sign_in_url": url}
            ),
        )
    except Exception:
        # Failed deliveries must not leave a usable credential behind.
        link.delete()
        raise


def redeem_link(digest):
    with transaction.atomic():
        now = timezone.now()
        link = (
            SignInLink.objects.select_for_update()
            .filter(
                digest=digest,
                used_at__isnull=True,
                expires_at__gt=now,
            )
            .first()
        )
        if not link:
            return None
        # The conditional UPDATE is also the single-use arbiter on SQLite.
        if not SignInLink.objects.filter(pk=link.pk, used_at__isnull=True).update(
            used_at=now
        ):
            return None
        User = get_user_model()
        user, _ = User.objects.get_or_create(
            email__iexact=link.email,
            defaults={
                "email": link.email,
                "username": uuid.uuid4().hex,
                "active": True,
            },
        )
        user = User.objects.select_for_update().get(pk=user.pk)
        if not user.is_active:
            return None
        user.is_verified = True
        if user.has_usable_password():
            user.set_unusable_password()
        user.save()
        EmailAddress.objects.update_or_create(
            user=user,
            email=user.email,
            defaults={"verified": True, "primary": True},
        )
        return user, link.redirect_to


def destination(request, user, redirect_to):
    redirect_to = safe_next(request, redirect_to)
    if has_fpl_setup(user) or (user.is_staff and redirect_to.startswith("/admin/")):
        return redirect_to
    request.session["onboarding_next"] = redirect_to
    return reverse("accounts:onboarding")
