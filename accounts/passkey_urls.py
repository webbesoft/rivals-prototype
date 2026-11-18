from django.urls import path
from django.views.generic import RedirectView

from .passkey_views import AddPasskeyView, PasskeyLoginView, RemovePasskeyView

urlpatterns = [
    # Allauth uses this reverse name if an email still needs verification.
    path(
        "",
        RedirectView.as_view(pattern_name="accounts:connections", permanent=False),
        name="mfa_index",
    ),
    path("login/", PasskeyLoginView.as_view(), name="mfa_login_webauthn"),
    path("add/", AddPasskeyView.as_view(), name="mfa_add_webauthn"),
    path(
        "<int:pk>/remove/",
        RemovePasskeyView.as_view(),
        name="mfa_remove_webauthn",
    ),
]
