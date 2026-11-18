"""Passkey ceremonies use allauth's WebAuthn validation and credential storage."""

import json
from functools import wraps

from allauth.account.models import Login
from allauth.mfa.webauthn.forms import AddWebAuthnForm, LoginWebAuthnForm
from allauth.mfa.webauthn.internal import auth, flows
from allauth.mfa.webauthn.views import (
    AddWebAuthnView,
    LoginWebAuthnView,
    RemoveWebAuthnView,
)
from django import forms
from django.conf import settings
from django.contrib import messages
from django.http import Http404, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse, reverse_lazy
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.generic.edit import FormView

from core.ai_privacy import sensitive_ai
from core.rate_limits import rate_limit

from .models import PasskeyChallenge
from .passkeys import claimed_challenge, issue_challenge
from .passwordless import safe_next


def passkeys_enabled(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not settings.PASSWORDLESS_AUTH_ENABLED:
            raise Http404
        if request.user.is_authenticated and not request.user.is_verified:
            return redirect("accounts:login")
        return view(request, *args, **kwargs)

    return wrapped


passkey_decorators = [
    sensitive_ai,
    never_cache,
    sensitive_post_parameters(),
    passkeys_enabled,
]


class AddPasskeyForm(AddWebAuthnForm):
    # Every saved credential must work as a standalone, PIN/biometric-protected
    # passkey. The server also checks the authenticator's user-verification flag.
    passwordless = forms.BooleanField(
        required=True, initial=True, widget=forms.HiddenInput
    )
    challenge_id = forms.UUIDField(widget=forms.HiddenInput)

    def clean(self):
        if self.errors:
            return self.cleaned_data
        with claimed_challenge(
            self.cleaned_data["challenge_id"],
            PasskeyChallenge.Purpose.REGISTER,
            self.user,
        ):
            return super().clean()


class PasskeyLoginForm(LoginWebAuthnForm):
    challenge_id = forms.UUIDField(widget=forms.HiddenInput)

    def clean_credential(self):
        # Credential cleaning runs before later fields in the parent form.
        challenge_id = self.fields["challenge_id"].clean(self.data.get("challenge_id"))
        with claimed_challenge(challenge_id, PasskeyChallenge.Purpose.LOGIN):
            return super().clean_credential()


@method_decorator(passkey_decorators, name="dispatch")
class AddPasskeyView(AddWebAuthnView):
    form_class = AddPasskeyForm

    def get_context_data(self, **kwargs):
        # Keep Django's bound form; allauth's override starts an unwanted
        # passwordless=False ceremony and discards the supplied form context.
        context = FormView.get_context_data(self, **kwargs)
        context["js_data"] = {
            "creation_options": auth.begin_registration(self.request.user, True)
        }
        challenge = issue_challenge(
            self.request, PasskeyChallenge.Purpose.REGISTER, self.request.user
        )
        context["js_data"]["challenge_id"] = str(challenge.pk)
        return context

    def get_success_url(self):
        return reverse("accounts:connections")


@method_decorator(passkey_decorators, name="dispatch")
class RemovePasskeyView(RemoveWebAuthnView):
    success_url = reverse_lazy("accounts:connections")


@method_decorator(passkey_decorators, name="dispatch")
@method_decorator(rate_limit("login"), name="dispatch")
class PasskeyLoginView(LoginWebAuthnView):
    form_class = PasskeyLoginForm

    def get(self, request, *args, **kwargs):
        response = super().get(request, *args, **kwargs)
        if isinstance(response, JsonResponse):
            challenge = issue_challenge(request, PasskeyChallenge.Purpose.LOGIN)
            return JsonResponse(
                {**json.loads(response.content), "challenge_id": str(challenge.pk)}
            )
        return response

    def form_valid(self, form):
        authenticator = form.cleaned_data["credential"]
        user = authenticator.user
        if not user.is_active or not user.is_verified:
            messages.error(self.request, "Unable to sign in. Please use an email link.")
            return redirect("accounts:login")
        login = Login(
            user=user,
            redirect_url=safe_next(self.request, self.request.POST.get("next")),
        )
        return flows.perform_passwordless_login(self.request, authenticator, login)
