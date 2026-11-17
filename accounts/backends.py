from django.conf import settings
from django.contrib.auth.backends import ModelBackend
from asgiref.sync import sync_to_async


class AccountBackend(ModelBackend):
    """Keep session/permission support while retiring password authentication."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        if settings.PASSWORDLESS_AUTH_ENABLED:
            return None
        if username:
            from django.contrib.auth import get_user_model

            existing = (
                get_user_model().objects.filter(email__iexact=username.strip()).first()
            )
            if existing:
                username = existing.email
        return super().authenticate(request, username, password, **kwargs)

    async def aauthenticate(self, request, username=None, password=None, **kwargs):
        # ModelBackend's async implementation does not call authenticate().
        return await sync_to_async(self.authenticate)(
            request, username, password, **kwargs
        )
