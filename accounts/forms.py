from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from rivals.services.fpl_api_service import FplApiService
from rivals.services.init_user_service import InitUserService

User = get_user_model()


class SignUpForm(UserCreationForm):
    fpl_id = forms.IntegerField(
        required=True,
        widget=forms.TextInput(attrs={"placeholder": "e.g. 1234567"}),
        help_text="Your Fantasy Premier League team ID",
    )
    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(attrs={"placeholder": "your@email.com"}),
    )

    class Meta:
        model = User
        fields = ("fpl_id", "email", "password1", "password2")

    def clean_email(self):
        email = self.cleaned_data.get("email")
        if User.objects.filter(email=email).exists():
            raise ValidationError("A user with this email already exists.")
        return email

    def clean_fpl_id(self):
        fpl_id = self.cleaned_data.get("fpl_id")
        if not fpl_id:
            raise ValidationError("FPL Team ID is required.")
        return fpl_id

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data["email"]
        user.username = self.cleaned_data["email"]
        user.fpl_id = self.cleaned_data["fpl_id"]

        if commit:
            # Validate FPL ID and fetch basic info
            fpl_api_service = FplApiService()
            basic_info = fpl_api_service.fetch_team_basic_info(user.fpl_id)

            if not basic_info:
                raise ValidationError("Invalid FPL Team ID. Please try again.")

            # Set basic user info from FPL
            user.team_name = basic_info["name"]
            user.manager_name = (
                basic_info["player_first_name"] + " " + basic_info["player_last_name"]
            )
            user.active = True
            user.is_verified = False  # Explicitly set to False until email is verified

            # Save user first
            user.save()

            # Send verification email immediately
            import logging
            import json
            from .tasks import init_user_data_task
            from .emails import EmailService

            logger = logging.getLogger(__name__)

            try:
                token = default_token_generator.make_token(user)
                uid = urlsafe_base64_encode(force_bytes(user.pk))
                verify_path = reverse("accounts:verify_email", args=[uid, token])
                verify_url = f"{settings.SITE_URL.rstrip('/')}{verify_path}"

                # Use EmailService for HTML email
                if EmailService.send_verification_email(user, verify_url):
                    logger.info(f"Verification email sent to {user.email}")
                else:
                    logger.warning(f"EmailService returned False for {user.email}")
            except Exception as e:
                # Log the error but don't block user creation
                logger.error(
                    f"Failed to send verification email to {user.email}: {e}",
                    exc_info=True,
                )

            # Queue the InitUserService to run in the background
            try:
                basic_info_json = json.dumps(basic_info)

                # Check if we should run async (background task) or sync (immediately)
                if getattr(settings, "BACKGROUND_TASK_RUN_ASYNC", False):
                    # Run asynchronously via django-background-tasks
                    init_user_data_task(user.id, basic_info_json)
                else:
                    # Run synchronously for development/debugging
                    init_user_data_task.now(user.id, basic_info_json)

            except Exception as e:
                logger.error(
                    f"Failed to process initialization for user {user.id}: {e}",
                    exc_info=True,
                )

        return user


class LoginForm(AuthenticationForm):
    username = forms.EmailField(
        widget=forms.EmailInput(
            attrs={
                "class": "w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500",
                "placeholder": "Enter your email",
            }
        )
    )
    password = forms.CharField(
        widget=forms.PasswordInput(
            attrs={
                "class": "w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500",
                "placeholder": "Enter your password",
            }
        )
    )


class PasswordResetForm(forms.Form):
    email = forms.EmailField(
        widget=forms.EmailInput(
            attrs={
                "class": "w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500",
                "placeholder": "Enter your email",
            }
        )
    )
