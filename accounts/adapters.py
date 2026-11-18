import time

from allauth.account.adapter import DefaultAccountAdapter
from allauth.account.internal.flows.login import get_authentication_records
from allauth.account.models import EmailAddress
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse

from .passwordless import destination


def _google_owns_email(sociallogin, email):
    """Use claims returned by Google, never an OAuth request's `hd` hint."""
    if sociallogin.account.provider != "google":
        return False
    claims = sociallogin.account.extra_data
    provider_email = claims.get("email")
    email = email.casefold()
    if not isinstance(provider_email, str) or provider_email.casefold() != email:
        return False
    if not any(
        address.verified and address.email.casefold() == email
        for address in sociallogin.email_addresses
    ):
        return False
    domain = email.rpartition("@")[2]
    if domain == "gmail.com":
        return True
    hosted_domain = claims.get("hd")
    return (
        bool(domain)
        and isinstance(hosted_domain, str)
        and hosted_domain.casefold() == domain
    )


class AccountAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request):
        # Local accounts are created only after redeeming an email challenge.
        return False

    def post_login(self, request, user, **kwargs):
        response = super().post_login(request, user, **kwargs)
        if settings.PASSWORDLESS_AUTH_ENABLED and response.status_code == 302:
            return redirect(destination(request, user, response.url))
        return response

    def get_login_stages(self):
        # Passkeys are an alternative sign-in method. Registering a key must
        # not add another challenge to Google sign-in or email recovery.
        return [
            stage
            for stage in super().get_login_stages()
            if not stage.startswith("allauth.mfa.stages.")
        ]

    def is_reauthentication_required(self, *, app, **kwargs):
        if app == "allauth.mfa" and self.request.user.is_authenticated:
            records = get_authentication_records(self.request, self.request.user)
            return not any(
                record.get("method") in {"code", "socialaccount", "mfa"}
                and time.time() - record["at"]
                < settings.ACCOUNT_REAUTHENTICATION_TIMEOUT
                for record in records
            )
        return super().is_reauthentication_required(app=app, **kwargs)

    def get_reauthentication_methods(self, user):
        if settings.PASSWORDLESS_AUTH_ENABLED and user.is_authenticated:
            return [{"url": reverse("accounts:reauthenticate")}]
        return super().get_reauthentication_methods(user)


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    def is_open_for_signup(self, request, sociallogin):
        return settings.PASSWORDLESS_AUTH_ENABLED

    def can_authenticate_by_email(self, login, email):
        # Allauth performs email lookup before pre_social_login(). Restrict
        # that lookup itself so a stale third-party email claim cannot log in.
        if not settings.PASSWORDLESS_AUTH_ENABLED or not (
            settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET
        ):
            return False
        return _google_owns_email(login, email) and super().can_authenticate_by_email(
            login, email
        )

    def pre_social_login(self, request, sociallogin):
        if not settings.PASSWORDLESS_AUTH_ENABLED or not (
            settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET
        ):
            raise ImmediateHttpResponse(redirect("accounts:login"))
        verified = [
            address.email.lower()
            for address in sociallogin.email_addresses
            if address.verified
        ]
        if not verified or sociallogin.user.email.lower() not in verified:
            messages.info(
                request,
                "Use an email sign-in link to verify your address, then connect Google in your profile.",
            )
            raise ImmediateHttpResponse(redirect("accounts:login"))
        if sociallogin.state.get("process") == "connect":
            if (
                not request.user.is_authenticated
                or not request.user.is_verified
                or request.user.email.lower() not in verified
            ):
                messages.error(
                    request,
                    "Connect a Google account with the same verified email as your RivalSpy account.",
                )
                raise ImmediateHttpResponse(redirect("accounts:login"))
        elif sociallogin.is_existing:
            if not sociallogin.user.is_active:
                raise ImmediateHttpResponse(redirect("accounts:login"))
            sociallogin.user.is_verified = True
            sociallogin.user.save(update_fields=["is_verified"])
            EmailAddress.objects.update_or_create(
                user=sociallogin.user,
                email=sociallogin.user.email,
                defaults={"verified": True, "primary": True},
            )
        elif not _google_owns_email(sociallogin, sociallogin.user.email):
            # This also covers first-time signups, so an old Google account
            # cannot register someone else's reassigned third-party address.
            messages.info(
                request,
                "For this address, sign in with an email link first, then connect Google from your profile.",
            )
            raise ImmediateHttpResponse(redirect("accounts:login"))

    def save_user(self, request, sociallogin, form=None):
        sociallogin.user.email = sociallogin.user.email.strip().lower()
        sociallogin.user.is_verified = True
        sociallogin.user.active = True
        return super().save_user(request, sociallogin, form)

    def get_connect_redirect_url(self, request, socialaccount):
        return "/accounts/connections/"

    def on_authentication_error(
        self, request, provider, error=None, exception=None, extra_context=None
    ):
        messages.error(
            request,
            "Google sign-in was cancelled or could not be completed. Try again or use an email link.",
        )
        raise ImmediateHttpResponse(redirect("accounts:login"))
