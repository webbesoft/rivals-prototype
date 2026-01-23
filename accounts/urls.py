from django.contrib import admin
from django.urls import path

from accounts import views

app_name = "accounts"

urlpatterns = [
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
]
