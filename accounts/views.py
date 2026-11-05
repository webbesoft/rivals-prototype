from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode

from .forms import LoginForm, PasswordResetForm, SignUpForm, UpdateFplIdForm
from .tasks import send_verification_email_task

User = get_user_model()


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

            next_url = request.GET.get("next", "rivals:dashboard")
            messages.success(request, f"Welcome back, {user.manager_name}!")
            return redirect(next_url)
    else:
        form = LoginForm()

    return render(request, "accounts/login.html", {"form": form})


@login_required
def logout_view(request):
    logout(request)
    messages.success(request, "You have been logged out successfully.")
    return redirect("core:index")


def password_reset_view(request):
    if request.method == "POST":
        form = PasswordResetForm(request.POST)
        if form.is_valid():
            email = form.cleaned_data["email"]
            try:
                user = User.objects.get(email=email)
                # Generate reset token
                token = default_token_generator.make_token(user)
                uid = urlsafe_base64_encode(force_bytes(user.pk))

                # Send reset email (using Celery task)
                reset_url = request.build_absolute_uri(
                    f"/accounts/reset-password/{uid}/{token}/"
                )
                # send_password_reset_email.delay(user.id, reset_url)

                messages.success(
                    request, "Password reset instructions have been sent to your email."
                )
                return redirect("accounts:login")
            except User.DoesNotExist:
                messages.error(request, "No user found with this email address.")
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
def update_fpl_id_view(request):
    """Handle FPL ID update form submission."""
    import json
    from .tasks import init_user_data_task

    if request.method != "POST":
        return redirect("accounts:profile")

    form = UpdateFplIdForm(request.POST)
    if form.is_valid():
        new_fpl_id = str(form.cleaned_data["fpl_id"])
        fpl_data = form.fpl_data  # already fetched in clean_fpl_id

        # Update user profile fields (but keep the existing team link for now)
        request.user.fpl_id = new_fpl_id
        request.user.team_name = fpl_data.get("name", "")
        request.user.manager_name = (
            fpl_data.get("player_first_name", "")
            + " "
            + fpl_data.get("player_last_name", "")
        ).strip()
        request.user.save(update_fields=["fpl_id", "team_name", "manager_name"])

        # Re-run InitUserService to create/link new team and leagues.
        # Only unlink the old team once we know the task will be dispatched
        # (or has completed synchronously) so the existing link stays intact
        # on failure.
        try:
            basic_info_json = json.dumps(fpl_data)
            if getattr(
                __import__("django.conf", fromlist=["settings"]).settings,
                "BACKGROUND_TASK_RUN_ASYNC",
                False,
            ):
                # Async path: unlink now; the task will create the new link
                request.user.team = None
                request.user.save(update_fields=["team"])
                init_user_data_task(request.user.id, basic_info_json)
            else:
                # Synchronous path: run first; only unlink on success
                init_user_data_task.now(request.user.id, basic_info_json)
                request.user.team = None
                request.user.save(update_fields=["team"])
        except Exception as e:
            import logging

            logging.getLogger(__name__).error(
                f"Failed to reinitialise user {request.user.id} after FPL ID update: {e}",
                exc_info=True,
            )

        messages.success(
            request,
            f"FPL team updated to \"{fpl_data.get('name', '')}\" successfully! "
            "Your leagues and stats are being refreshed.",
        )
        return redirect("rivals:dashboard")

    # Form invalid — re-render profile page with errors
    return render(request, "accounts/profile.html", {"form": form})
