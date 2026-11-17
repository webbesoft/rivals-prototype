from rest_framework.permissions import IsAuthenticated
from django.conf import settings
from rest_framework.exceptions import PermissionDenied
from accounts.onboarding import has_fpl_setup


class IsVerified(IsAuthenticated):
    message = "Verify your email before accessing your account."

    def has_permission(self, request, view):
        return (
            super().has_permission(request, view)
            and request.user.is_active
            and request.user.is_verified
        )


class IsOnboarded(IsVerified):
    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        if settings.PASSWORDLESS_AUTH_ENABLED and not has_fpl_setup(request.user):
            raise PermissionDenied(
                {
                    "code": "onboarding_required",
                    "onboarding_url": "/accounts/onboarding/fpl/",
                }
            )
        return True
