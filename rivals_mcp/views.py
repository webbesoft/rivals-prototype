from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from oauth2_provider.models import AccessToken, Grant, RefreshToken
from oauth2_provider.views import (
    AuthorizationView,
    DynamicClientRegistrationView,
    RevokeTokenView,
    TokenView,
)

from core.ai_privacy import sensitive_ai
from core.rate_limits import rate_limit

from .models import Connection
from .oauth import resource_url


@method_decorator(sensitive_ai, name="dispatch")
@method_decorator(never_cache, name="dispatch")
class ConsentView(AuthorizationView):
    template_name = "accounts/mcp_consent.html"

    def dispatch(self, request, *args, **kwargs):
        if not settings.MCP_ENABLED:
            return JsonResponse({"error": "temporarily_unavailable"}, status=503)
        if request.user.is_authenticated:
            if not request.user.is_active or not request.user.is_verified:
                return redirect("accounts:verification_pending")
            params = request.POST if request.method == "POST" else request.GET
            if (
                params.get("resource") != resource_url()
                or params.get("response_type") != "code"
                or params.get("code_challenge_method") != "S256"
                or params.get("scope", "rivals:read") != "rivals:read"
            ):
                return JsonResponse(
                    {
                        "error": "invalid_request",
                        "error_description": "Read access, S256 PKCE and the Rivals resource address are required.",
                    },
                    status=400,
                )
        return super().dispatch(request, *args, **kwargs)


@method_decorator(rate_limit("mcp_register"), name="dispatch")
@method_decorator(sensitive_ai, name="dispatch")
class RegisterView(DynamicClientRegistrationView):
    def post(self, request, *args, **kwargs):
        if not settings.MCP_ENABLED:
            return JsonResponse({"error": "temporarily_unavailable"}, status=503)
        return super().post(request, *args, **kwargs)


@method_decorator(rate_limit("mcp_token"), name="dispatch")
@method_decorator(sensitive_ai, name="dispatch")
@method_decorator(never_cache, name="dispatch")
class OAuthTokenView(TokenView):
    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        # Toolkit errors raised during token persistence carry a JSON body but
        # omit its content type, unlike errors from initial validation.
        response["Content-Type"] = "application/json"
        return response


@method_decorator(rate_limit("mcp_token"), name="dispatch")
@method_decorator(sensitive_ai, name="dispatch")
@method_decorator(never_cache, name="dispatch")
class OAuthRevokeView(RevokeTokenView):
    pass


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def settings_view(request):
    if request.method == "POST":
        with transaction.atomic():
            connection = (
                Connection.objects.select_for_update()
                .filter(
                    pk=(
                        request.POST.get("connection")
                        if str(request.POST.get("connection", "")).isdigit()
                        else None
                    ),
                    user=request.user,
                    revoked_at__isnull=True,
                )
                .first()
            )
            if connection:
                connection.revoked_at = timezone.now()
                connection.save(update_fields=["revoked_at"])
                scope = {
                    "user": request.user,
                    "application_id": connection.application_id,
                }
                # Remove codes and every refresh generation, including expired access tokens.
                Grant.objects.filter(**scope).delete()
                RefreshToken.objects.filter(**scope).delete()
                AccessToken.objects.filter(**scope).delete()
                messages.success(
                    request,
                    "Assistant disconnected. It can no longer read your Rivals data.",
                )
        return redirect("accounts:mcp")
    connections = (
        Connection.objects.filter(user=request.user, revoked_at__isnull=True)
        .select_related("application")
        .order_by("-authorized_at")
    )
    return render(
        request,
        "accounts/mcp.html",
        {
            "connection_address": resource_url(),
            "connections": connections,
            "mcp_enabled": settings.MCP_ENABLED,
        },
    )
