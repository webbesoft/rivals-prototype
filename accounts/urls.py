from django.contrib.auth.views import (
    PasswordResetCompleteView,
    PasswordResetConfirmView,
)
from django.urls import path, reverse_lazy

from accounts import ai_views, digest_views, views

from .tokens import password_reset_token_generator

app_name = "accounts"

urlpatterns = [
    path(
        "reset-password/<uidb64>/<token>/",
        PasswordResetConfirmView.as_view(
            template_name="accounts/password_reset_confirm.html",
            token_generator=password_reset_token_generator,
            success_url=reverse_lazy("accounts:password_reset_complete"),
        ),
        name="password_reset_confirm",
    ),
    path(
        "password-reset/complete/",
        PasswordResetCompleteView.as_view(
            template_name="accounts/password_reset_complete.html",
        ),
        name="password_reset_complete",
    ),
    path("digests/", digest_views.digest_settings, name="digests"),
    path(
        "digests/<uuid:digest_id>/", digest_views.digest_detail, name="digests.detail"
    ),
    path(
        "digests/unsubscribe/<str:token>/",
        digest_views.unsubscribe,
        name="digests.unsubscribe",
    ),
    path("gemini/", ai_views.gemini_settings, name="gemini"),
    path("signup/", views.signup_view, name="signup"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("password-reset/", views.password_reset_view, name="password_reset"),
    path("verify-email/<uidb64>/<token>/", views.verify_email, name="verify_email"),
    path(
        "verification-pending/",
        views.verification_pending_view,
        name="verification_pending",
    ),
    path(
        "resend-verification/",
        views.resend_verification_email,
        name="resend_verification",
    ),
    path("profile/", views.profile_view, name="profile"),
    path("profile/update-fpl-id/", views.update_fpl_id_view, name="update_fpl_id"),
]
