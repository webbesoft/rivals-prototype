from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_http_methods

from core.ai_privacy import sensitive_ai
from rivals.models import AICredential
from rivals.services.ai_credentials import (
    AIUnavailable,
    configured,
    connect,
    disconnect,
)


class GeminiConnectionForm(forms.Form):
    api_key = forms.RegexField(
        regex=r"^[A-Za-z0-9_-]+$",
        min_length=16,
        max_length=256,
        label="Gemini API key",
        widget=forms.PasswordInput(
            render_value=False,
            attrs={
                "autocomplete": "new-password",
                "class": "border border-gray-300 p-3 w-full",
                "spellcheck": "false",
                "autocapitalize": "none",
            },
        ),
    )


@login_required
@require_http_methods(["GET", "POST"])
@never_cache
@sensitive_post_parameters("api_key")
@sensitive_ai
@sensitive_variables()
def gemini_settings(request):
    from accounts.digest_views import briefing_settings_context

    form = GeminiConnectionForm()
    if request.method == "POST":
        if request.POST.get("action") == "remove":
            disconnect(request.user)
            messages.success(
                request,
                "AI briefings disconnected. Your saved key and briefings have been removed.",
            )
            return redirect("accounts:gemini")
        if request.POST.get("action") != "connect":
            messages.error(request, "Choose Enable AI briefings or Remove key.")
        else:
            form = GeminiConnectionForm(request.POST)
            if form.is_valid():
                try:
                    connect(request.user, form.cleaned_data["api_key"])
                except AIUnavailable as error:
                    messages.error(request, str(error))
                except Exception:
                    messages.error(
                        request, "AI briefings couldn't be enabled. Please try again."
                    )
                else:
                    messages.success(
                        request,
                        "Gemini connected. Set when to receive your briefing below.",
                    )
                    return redirect("accounts:gemini")
                form = GeminiConnectionForm()
    return render(
        request,
        "accounts/gemini.html",
        {
            **briefing_settings_context(request.user),
            "connection_form": form,
            "gemini_configured": configured(),
            "gemini_connected": AICredential.objects.filter(user=request.user).exists(),
        },
    )
