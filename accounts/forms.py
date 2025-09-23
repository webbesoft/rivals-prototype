from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.core.exceptions import ValidationError

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
            fpl_api_service = FplApiService()
            basic_info = fpl_api_service.fetch_team_basic_info(user.fpl_id)

            if not basic_info:
                raise ValidationError("Invalid FPL Team ID. Please try again.")

            user.team_name = basic_info["name"]
            user.manager_name = (
                basic_info["player_first_name"] + " " + basic_info["player_last_name"]
            )
            user.active = True

            user.save()
            user_init_service = InitUserService(user)
            user_init_service.save_user_info(basic_info=basic_info)

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
