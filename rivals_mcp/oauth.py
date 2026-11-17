"""Rivals-specific policy around Django OAuth Toolkit's protocol implementation."""

import json

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from oauth2_provider.models import Grant, RefreshToken
from oauth2_provider.oauth2_validators import OAuth2Validator
from oauthlib.oauth2.rfc6749.errors import CustomOAuth2Error, InvalidGrantError

from .models import Connection


def resource_url():
    return settings.SITE_URL.rstrip("/") + "/mcp"


class RivalsOAuthValidator(OAuth2Validator):
    def save_authorization_code(self, client_id, code, request, *args, **kwargs):
        from accounts.onboarding import has_fpl_setup

        if settings.PASSWORDLESS_AUTH_ENABLED and not has_fpl_setup(request.user):
            raise InvalidGrantError()
        if not request.user.is_active or not request.user.is_verified:
            raise InvalidGrantError()
        if request.resource != [resource_url()]:
            raise CustomOAuth2Error(error="invalid_target")
        with transaction.atomic():
            super().save_authorization_code(client_id, code, request, *args, **kwargs)
            Connection.objects.update_or_create(
                user=request.user,
                application=request.client,
                defaults={
                    "authorized_at": timezone.now(),
                    "revoked_at": None,
                    "last_used_at": None,
                },
            )

    def _save_bearer_token(self, token, request, *args, **kwargs):
        from accounts.onboarding import has_fpl_setup

        if settings.PASSWORDLESS_AUTH_ENABLED and not has_fpl_setup(request.user):
            raise InvalidGrantError()
        connection = (
            Connection.objects.select_for_update()
            .filter(
                user=request.user, application=request.client, revoked_at__isnull=True
            )
            .first()
        )
        if not connection or not request.user.is_active or not request.user.is_verified:
            raise InvalidGrantError()
        # Initial validation precedes this transaction. Disconnect may have
        # deleted the credential since then, even if fresh consent reopened the
        # same Connection. Recheck under the lock shared with disconnect.
        if request.grant_type == "refresh_token":
            cached = getattr(request, "refresh_token_instance", None)
            refresh = (
                RefreshToken.objects.select_for_update()
                .filter(
                    pk=cached.pk if cached else None,
                    user=request.user,
                    application=request.client,
                    revoked__isnull=True,
                )
                .first()
            )
            if not refresh or not self.validate_refresh_token(
                request.refresh_token, request.client, request
            ):
                raise InvalidGrantError()
        elif request.grant_type == "authorization_code":
            if (
                not Grant.objects.select_for_update()
                .filter(
                    code=request.code,
                    user=request.user,
                    application=request.client,
                    expires__gt=timezone.now(),
                )
                .exists()
            ):
                raise InvalidGrantError()
        else:
            raise InvalidGrantError()
        return super()._save_bearer_token(token, request, *args, **kwargs)

    def _check_and_set_request_resource(self, request):
        super()._check_and_set_request_resource(request)
        if request.resource != [resource_url()]:
            raise CustomOAuth2Error(error="invalid_target")


class RegistrationPermission:
    def has_permission(self, request):
        # DCR deliberately accepts anonymous clients. No metadata URL is fetched.
        try:
            data = json.loads(request.body)
            return (
                isinstance(data, dict)
                and set(data.get("grant_types", ["authorization_code"]))
                <= {"authorization_code", "refresh_token"}
                and "authorization_code"
                in data.get("grant_types", ["authorization_code"])
                and len(data.get("redirect_uris", [])) <= 5
                and len(data.get("client_name", "")) <= 100
            )
        except (ValueError, TypeError):
            return False
