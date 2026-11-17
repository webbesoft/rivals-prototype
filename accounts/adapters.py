from allauth.account.adapter import DefaultAccountAdapter
from allauth.account.models import EmailAddress
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.shortcuts import redirect

from .passwordless import destination


class AccountAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request):
        # Local accounts are created only after redeeming an email challenge.
        return False

    def post_login(self, request, user, **kwargs):
        response = super().post_login(request, user, **kwargs)
        if settings.PASSWORDLESS_AUTH_ENABLED and response.status_code == 302:
            return redirect(destination(request, user, response.url))
        return response


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    def is_open_for_signup(self, request, sociallogin):
        return settings.PASSWORDLESS_AUTH_ENABLED

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
        elif (
            not sociallogin.is_existing
            and get_user_model()
            .objects.filter(email__iexact=sociallogin.user.email)
            .exists()
        ):
            messages.info(
                request,
                "Sign in with an email link first, then connect Google from your profile.",
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
