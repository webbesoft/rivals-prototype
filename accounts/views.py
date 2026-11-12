from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.tokens import default_token_generator
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.encoding import force_str
from django.utils.http import (
    url_has_allowed_host_and_scheme,
    urlsafe_base64_decode,
)
from django.views.decorators.http import require_POST

from core.rate_limits import rate_limit

from .forms import LoginForm, PasswordResetForm, SignUpForm, UpdateFplIdForm
from .tasks import send_verification_email_task

User = get_user_model()


@rate_limit("signup")
def signup_view(request):
    if request.user.is_authenticated:
        return redirect("rivals:dashboard")

    if request.method == "POST":
        form = SignUpForm(request.POST)
        if form.is_valid():
            user = form.save()
            # Store user email in session for verification page
            request.session["pending_verification_email"] = user.email
            messages.success(
                request,
                "Account created! Please check your email to verify your account.",
            )
            return redirect("accounts:verification_pending")
    else:
        form = SignUpForm()

    return render(request, "accounts/signup.html", {"form": form})


@rate_limit("login", identity_field="username")
def login_view(request):
    if request.user.is_authenticated:
        return redirect("rivals:dashboard")

    if request.method == "POST":
        form = LoginForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            if not user.is_verified:
                # Store email in session and redirect to verification page
                request.session["pending_verification_email"] = user.email
                messages.warning(
                    request,
                    "Your email address is not verified. Please check your inbox.",
                )
                return redirect("accounts:verification_pending")
            login(request, user)

            next_url = request.POST.get("next") or request.GET.get("next", "")
            if not url_has_allowed_host_and_scheme(
                next_url,
                allowed_hosts={request.get_host()},
                require_https=request.is_secure(),
            ):
                next_url = reverse("rivals:dashboard")
            messages.success(request, f"Welcome back, {user.manager_name}!")
            return redirect(next_url)
    else:
        form = LoginForm()

    return render(
        request,
        "accounts/login.html",
        {"form": form, "next": request.POST.get("next") or request.GET.get("next", "")},
    )


@login_required
@require_POST
def logout_view(request):
    logout(request)
    messages.success(request, "You have been logged out successfully.")
    return redirect("core:index")


@rate_limit("password_reset", identity_field="email")
def password_reset_view(request):
    if request.method == "POST":
        form = PasswordResetForm(request.POST)
        if form.is_valid():
            import logging
            from urllib.parse import urlsplit

            from .tokens import password_reset_token_generator

            origin = urlsplit(settings.SITE_URL)
            try:
                form.save(
                    domain_override=origin.netloc,
                    use_https=origin.scheme == "https",
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    email_template_name="accounts/emails/password_reset.txt",
                    subject_template_name="accounts/emails/password_reset_subject.txt",
                    token_generator=password_reset_token_generator,
                )
            except Exception:
                logging.getLogger(__name__).exception(
                    "Password reset email delivery failed"
                )
            messages.success(
                request,
                "If an eligible account exists, password reset instructions will be sent to that email address.",
            )
            return redirect("accounts:login")
    else:
        form = PasswordResetForm()

    return render(request, "accounts/password_reset.html", {"form": form})


def verify_email(request, uidb64, token):
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        user = None

    if user and default_token_generator.check_token(user, token):
        user.is_verified = True
        user.save()

        # Send welcome email after verification
        # send_welcome_email.delay(user.id)

        messages.success(
            request, "Email verified successfully! Welcome to our platform."
        )
        return redirect("accounts:login")
    else:
        messages.error(request, "Invalid verification link.")
        return redirect("accounts:signup")


def verification_pending_view(request):
    """
    Show verification pending page with option to resend email.
    """
    # Get email from session (set during signup or failed login)
    email = request.session.get("pending_verification_email")

    if not email:
        # If no email in session, redirect to login
        messages.info(request, "Please log in to continue.")
        return redirect("accounts:login")

    return render(request, "accounts/verification_pending.html", {"email": email})


@rate_limit("resend", session_field="pending_verification_email")
def resend_verification_email(request):
    """
    Resend verification email to the user.
    """
    if request.method != "POST":
        return redirect("accounts:verification_pending")

    email = request.session.get("pending_verification_email")

    if not email:
        messages.error(request, "Session expired. Please try logging in again.")
        return redirect("accounts:login")

    try:
        user = User.objects.get(email=email)

        if user.is_verified:
            messages.info(
                request, "Your email is already verified. You can log in now."
            )
            return redirect("accounts:login")

        # Check if we should run async (background task) or sync (immediately)
        if getattr(settings, "BACKGROUND_TASK_RUN_ASYNC", False):
            # Queue the email sending task async
            send_verification_email_task(user.id)
        else:
            # Run synchronously for development
            send_verification_email_task.now(user.id)

        messages.success(
            request, "Verification email has been resent. Please check your inbox."
        )
    except User.DoesNotExist:
        messages.error(request, "User not found. Please sign up again.")
        return redirect("accounts:signup")

    return redirect("accounts:verification_pending")


# ---------------------------------------------------------------------------
# Profile / FPL ID management
# ---------------------------------------------------------------------------


@login_required
def profile_view(request):
    """Display the user's profile and FPL settings."""
    from rivals.services.fpl_api_service import FplApiService

    form = UpdateFplIdForm(initial={"fpl_id": request.user.fpl_id})

    # Check if the live FPL season differs from the user's team season
    new_season_available = False
    if request.user.team and request.user.team.current_season:
        try:
            live_season = FplApiService().fetch_current_season()
            if live_season and live_season != request.user.team.current_season:
                new_season_available = True
        except Exception:
            pass

    return render(
        request,
        "accounts/profile.html",
        {
            "form": form,
            "new_season_available": new_season_available,
        },
    )


@login_required
@rate_limit("profile_update")
def update_fpl_id_view(request):
    """Handle FPL ID update form submission."""
    import json

    from .tasks import init_user_data_task

    if request.method != "POST":
        return redirect("accounts:profile")

    form = UpdateFplIdForm(request.POST)
    if form.is_valid():
        from django.db import transaction

        fpl_data = form.fpl_data
        try:
            # The snapshot is already fetched. No HTTP runs inside this transaction.
            with transaction.atomic():
                user = User.objects.select_for_update().get(pk=request.user.pk)
                if user.fpl_revision != request.user.fpl_revision:
                    raise ValueError("A newer team update has already completed")
                user.fpl_id = str(form.cleaned_data["fpl_id"])
                user.team_name = fpl_data.get("name", "")
                user.manager_name = (
                    fpl_data.get("player_first_name", "")
                    + " "
                    + fpl_data.get("player_last_name", "")
                ).strip()
                user.fpl_revision += 1
                user.save(
                    update_fields=[
                        "fpl_id",
                        "team_name",
                        "manager_name",
                        "fpl_revision",
                    ]
                )
                init_user_data_task.now(
                    user.pk, json.dumps(fpl_data), user.fpl_revision
                )
            messages.success(
                request, "FPL team updated. Your leagues and stats are being refreshed."
            )
            return redirect("rivals:dashboard")
        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                "FPL team switch failed for user %s", request.user.pk
            )
            messages.error(
                request,
                "Failed to refresh FPL team data. Your previous settings have been preserved.",
            )
            return redirect("accounts:profile")

    # Form invalid — re-render profile page with errors
    return render(request, "accounts/profile.html", {"form": form})
