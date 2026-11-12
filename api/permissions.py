from rest_framework.permissions import IsAuthenticated


class IsVerified(IsAuthenticated):
    message = "Verify your email before accessing your account."

    def has_permission(self, request, view):
        return super().has_permission(request, view) and request.user.is_verified
