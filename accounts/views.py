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

from .forms import LoginForm, PasswordResetForm, SignUpForm
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
