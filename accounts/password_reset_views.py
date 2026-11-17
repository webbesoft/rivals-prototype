from django.conf import settings
from django.contrib.auth.views import PasswordResetConfirmView
from django.shortcuts import redirect


class LegacyPasswordResetConfirmView(PasswordResetConfirmView):
    def dispatch(self, request, *args, **kwargs):
        if settings.PASSWORDLESS_AUTH_ENABLED:
            return redirect("accounts:login")
        return super().dispatch(request, *args, **kwargs)
