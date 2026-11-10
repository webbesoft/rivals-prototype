import uuid

from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.db import transaction
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from rivals.models import DeadlineDigest, DigestSubscription
from rivals.services.deadline_digest_service import (
    email_context,
    eligible,
    request_digest,
)


class DigestSettingsForm(forms.ModelForm):
    lead_hours = forms.TypedChoiceField(
        label="Send before the deadline",
        coerce=int,
        widget=forms.Select(attrs={"aria-describedby": "briefing-timing-help"}),
        choices=[
            (1, "1 hour"),
            (2, "2 hours"),
            (4, "4 hours"),
            (6, "6 hours"),
            (12, "12 hours"),
            (24, "24 hours"),
        ],
    )

    class Meta:
        model = DigestSubscription
        fields = ["enabled", "paused", "lead_hours"]
        widgets = {
            "enabled": forms.CheckboxInput(
                attrs={"role": "switch", "aria-describedby": "briefing-enabled-help"}
            ),
            "paused": forms.CheckboxInput(
                attrs={"role": "switch", "aria-describedby": "briefing-paused-help"}
            ),
        }
        labels = {
            "enabled": "Email me an AI briefing before each FPL deadline",
            "paused": "Pause briefing emails",
        }


def cancel_pending(user):
    DeadlineDigest.objects.filter(user=user, kind="scheduled").exclude(
        delivery_state__in=["sent", "sending", "ambiguous"]
    ).update(
        generation_state="skipped",
        delivery_state="skipped",
        lease_until=None,
        notice="Deadline digests are paused or disabled.",
    )


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def digest_settings(request):
    if request.method == "GET":
        return redirect("accounts:gemini")
    subscription, _ = DigestSubscription.objects.get_or_create(user=request.user)
    form = DigestSettingsForm(instance=subscription)
    if request.method == "POST":
        action = request.POST.get("action")
        if action in ("preview", "test"):
            try:
                record = request_digest(request.user, action)
                return redirect("accounts:digests.detail", digest_id=record.pk)
            except ValueError as error:
                messages.error(request, str(error))
        elif action == "save":
            form = DigestSettingsForm(request.POST, instance=subscription)
            if form.is_valid():
                if form.cleaned_data["enabled"] and not eligible(request.user):
                    form.add_error(
                        "enabled",
                        "Verify your email and link an active FPL team to enable deadline emails.",
                    )
                else:
                    with transaction.atomic():
                        get_user_model().objects.select_for_update().get(
                            pk=request.user.pk
                        )
                        saved = DigestSubscription.objects.select_for_update().get(
                            pk=subscription.pk
                        )
                        enabling = form.cleaned_data["enabled"] and not saved.enabled
                        for field in ("enabled", "paused", "lead_hours"):
                            setattr(saved, field, form.cleaned_data[field])
                        if enabling:
                            saved.consented_at = timezone.now()
                            saved.unsubscribe_token = uuid.uuid4()
                        saved.save()
                        if not saved.enabled or saved.paused:
                            cancel_pending(request.user)
                    messages.success(request, "Deadline email settings saved.")
                    return redirect("accounts:gemini")
        else:
            return HttpResponseBadRequest("Choose a digest action.")
    return render(
        request,
        "accounts/gemini.html",
        {
            **briefing_settings_context(request.user),
            "digest_form": form,
            "digest_eligible": eligible(request.user),
            "digests": DeadlineDigest.objects.filter(user=request.user).order_by(
                "-created_at"
            )[:10],
        },
    )


@login_required
@never_cache
@require_http_methods(["GET"])
def digest_detail(request, digest_id):
    record = get_object_or_404(DeadlineDigest, pk=digest_id, user=request.user)
    # A team change should not expose a previous team's saved report in the preview.
    context = {"digest": record}
    if (
        record.report
        and request.user.team_id == record.team_id
        and request.GET.get("email") == "1"
    ):
        return render(request, "emails/deadline_digest.html", email_context(record))
    return render(request, "accounts/digest_detail.html", context)


@never_cache
@require_http_methods(["GET", "POST"])
def unsubscribe(request, token):
    try:
        data = signing.loads(token, salt="deadline-unsubscribe")
        subscription = DigestSubscription.objects.select_related("user").get(
            user_id=data["user"],
            unsubscribe_token=data["revision"],
        )
    except (
        signing.BadSignature,
        DigestSubscription.DoesNotExist,
        KeyError,
        ValueError,
    ):
        return HttpResponseBadRequest(
            "This unsubscribe link is no longer valid. Manage deadline emails in your account settings."
        )
    done = False
    if request.method == "POST":
        with transaction.atomic():
            get_user_model().objects.select_for_update().get(pk=subscription.user_id)
            # Recheck the token under the consent lock.
            saved = DigestSubscription.objects.filter(
                pk=subscription.pk, unsubscribe_token=data["revision"]
            ).first()
            if not saved:
                return HttpResponseBadRequest(
                    "This unsubscribe link is no longer valid."
                )
            saved.enabled = False
            saved.save(update_fields=["enabled", "updated_at"])
            cancel_pending(subscription.user)
        done = True
    return render(request, "accounts/digest_unsubscribe.html", {"done": done})


def briefing_settings_context(user):
    from accounts.ai_views import GeminiConnectionForm
    from rivals.models import AICredential
    from rivals.services.ai_credentials import configured

    subscription, _ = DigestSubscription.objects.get_or_create(user=user)
    return {
        "connection_form": GeminiConnectionForm(),
        "gemini_configured": configured(),
        "gemini_connected": AICredential.objects.filter(user=user).exists(),
        "digest_form": DigestSettingsForm(instance=subscription),
        "digest_eligible": eligible(user),
        "digests": DeadlineDigest.objects.filter(user=user).order_by("-created_at")[
            :10
        ],
    }
