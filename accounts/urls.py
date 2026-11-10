from django.contrib import admin
from django.urls import path

from accounts import ai_views, digest_views, views

app_name = "accounts"

urlpatterns = [
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
