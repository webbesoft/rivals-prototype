import logging
from urllib.parse import urlencode

from allauth.account.internal.flows.login import record_authentication
from allauth.mfa.models import Authenticator
from allauth.socialaccount.models import SocialAccount
from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseGone
from django.shortcuts import redirect, render
from django.urls import NoReverseMatch, reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from core.ai_privacy import sensitive_ai
from core.rate_limits import rate_limit, reserve_request

from .forms import UpdateFplIdForm
from .models import SignInLink
from .onboarding import has_fpl_setup, save_fpl_setup
from .passwordless import (
    BACKEND,
    SENT_MESSAGE,
    destination,
    issue_link,
    redeem_link,
    safe_next,
    token_digest,
)

logger = logging.getLogger(__name__)


class EmailSignInForm(forms.Form):
    email = forms.EmailField(max_length=254)


@sensitive_ai
@never_cache
@sensitive_post_parameters()
@rate_limit("magic_link", identity_field="email")
@require_http_methods(["GET", "POST"])
def sign_in(request):
    if request.user.is_authenticated and request.user.is_verified:
        return redirect(destination(request, request.user, request.GET.get("next", "")))
    next_url = safe_next(
        request, request.POST.get("next") or request.GET.get("next", "")
    )
    form = EmailSignInForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            issue_link(form.cleaned_data["email"], next_url)
        except Exception:
            # Don't log the address, secret or exception payload from SMTP.
            logger.error("Sign-in email delivery failed")
        return render(request, "accounts/link_sent.html", {"message": SENT_MESSAGE})
    google_url = ""
    if settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET:
        try:
            google_url = reverse("google_login") + "?" + urlencode({"next": next_url})
        except NoReverseMatch:
            pass  # Email sign-in remains available without the optional provider.
    return render(
        request,
        "accounts/passwordless_login.html",
        {
            "form": form,
            "next": next_url,
            "google_url": google_url,
            "google_enabled": bool(google_url),
        },
    )


@sensitive_ai
@never_cache
@rate_limit("magic_redeem", methods=("GET",))
@require_http_methods(["GET"])
def magic_link(request, token):
    if not settings.PASSWORDLESS_AUTH_ENABLED:
        return HttpResponseGone("Passwordless sign-in is not enabled.")
    request.session["sign_in_digest"] = token_digest(token)
    response = redirect("accounts:magic_confirm")
    response["Referrer-Policy"] = "no-referrer"
    return response


@sensitive_ai
@never_cache
@rate_limit("magic_redeem")
@require_http_methods(["GET", "POST"])
def magic_confirm(request):
    if not settings.PASSWORDLESS_AUTH_ENABLED:
        return HttpResponseGone("Passwordless sign-in is not enabled.")
    if request.method == "POST":
        digest = request.session.get("sign_in_digest", "")
        challenge = SignInLink.objects.filter(digest=digest).first()
        # A different tab may have opened another link in the shared session.
        # Reject the old form without consuming or discarding the new challenge.
        matches = challenge and request.POST.get("challenge_id") == str(challenge.pk)
        result = redeem_link(digest) if matches else None
        if result:
            request.session.pop("sign_in_digest", None)
            user, next_url = result
            login(request, user, backend=BACKEND)
            record_authentication(request, user, method="code", reauthenticated=True)
            # Return to a confirmation form; never replay a sensitive POST
            # suspended by allauth's reauthentication decorator.
            request.session.pop("account_reauthentication_state", None)
            return redirect(destination(request, user, next_url))
        return render(
            request, "accounts/magic_confirm.html", {"invalid": True}, status=400
        )
    from django.utils import timezone

    challenge = SignInLink.objects.filter(
        digest=request.session.get("sign_in_digest", ""),
        used_at__isnull=True,
        expires_at__gt=timezone.now(),
    ).first()
    returning_user = (
        get_user_model()
        .objects.select_related("team")
        .filter(email__iexact=challenge.email, is_active=True, is_verified=True)
        .first()
        if challenge
        else None
    )
    auto_sign_in = bool(
        returning_user
        and has_fpl_setup(returning_user)
        and not request.session.get("account_reauthentication_state")
        and (not request.user.is_authenticated or request.user.pk == returning_user.pk)
    )
    return render(
        request,
        "accounts/magic_confirm.html",
        {
            "invalid": challenge is None,
            "email": challenge.email if challenge else "",
            "challenge_id": challenge.pk if challenge else "",
            "auto_sign_in": auto_sign_in,
        },
    )


@login_required
@never_cache
@rate_limit("profile_update")
@require_http_methods(["GET", "POST"])
def onboarding(request):
    if not request.user.is_verified:
        return redirect("accounts:login")
    if has_fpl_setup(request.user):
        return redirect(safe_next(request, request.session.pop("onboarding_next", "")))
    if request.method == "GET" and request.GET.get("next"):
        request.session["onboarding_next"] = safe_next(request, request.GET["next"])
    form = UpdateFplIdForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            save_fpl_setup(request.user, form.cleaned_data["fpl_id"], form.fpl_data)
        except Exception:
            logger.exception("FPL onboarding failed for user %s", request.user.pk)
            form.add_error(None, "Could not save your team. Please try again.")
        else:
            messages.success(
                request, "Team connected. Your leagues and stats are being refreshed."
            )
            return redirect(
                safe_next(request, request.session.pop("onboarding_next", ""))
            )
    return render(request, "accounts/onboarding.html", {"form": form})


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def connections(request):
    if not settings.PASSWORDLESS_AUTH_ENABLED:
        return redirect("accounts:profile")
    accounts = SocialAccount.objects.filter(user=request.user, provider="google")
    if request.method == "POST":
        # A verified primary email always remains available for magic-link recovery.
        accounts.filter(
            pk=(
                request.POST.get("account")
                if str(request.POST.get("account", "")).isdigit()
                else None
            )
        ).delete()
        messages.success(
            request, "Google disconnected. You can still sign in by email."
        )
        return redirect("accounts:connections")
    return render(
        request,
        "accounts/connections.html",
        {
            "connections": accounts,
            "passkeys": Authenticator.objects.filter(
                user=request.user, type=Authenticator.Type.WEBAUTHN
            ).order_by("created_at"),
            "google_enabled": bool(
                settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET
            ),
        },
    )


@login_required
@sensitive_ai
@never_cache
@require_http_methods(["GET", "POST"])
def reauthenticate(request):
    if not settings.PASSWORDLESS_AUTH_ENABLED or not request.user.is_verified:
        return redirect("accounts:login")
    next_url = safe_next(
        request,
        request.POST.get("next")
        or request.GET.get("next")
        or reverse("accounts:connections"),
    )
    if request.method == "POST":
        retry = reserve_request(request, "magic_link", identity=request.user.email)
        if retry:
            response = HttpResponse(
                "Too many requests. Please try again later.", status=429
            )
            response["Retry-After"] = str(retry)
            return response
        try:
            issue_link(request.user.email, next_url)
        except Exception:
            logger.error("Sign-in email delivery failed")
            return render(
                request,
                "accounts/reauthenticate.html",
                {
                    "next": next_url,
                    "delivery_error": "Could not send your confirmation email. Please wait a minute and try again.",
                },
            )
        return render(request, "accounts/link_sent.html", {"message": SENT_MESSAGE})
    return render(request, "accounts/reauthenticate.html", {"next": next_url})


def admin_login(request, **kwargs):
    if settings.PASSWORDLESS_AUTH_ENABLED:
        if request.user.is_authenticated and not request.user.is_staff:
            return render(request, "accounts/admin_access_denied.html", status=403)
        return redirect(
            reverse("accounts:login")
            + "?"
            + urlencode({"next": request.GET.get("next", reverse("admin:index"))})
        )
    from django.contrib import admin

    return admin.site.login(request, **kwargs)
