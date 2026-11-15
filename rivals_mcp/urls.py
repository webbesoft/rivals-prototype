from django.urls import path
from oauth2_provider.views import (
    DynamicClientRegistrationManagementView,
    OAuthProtectedResourceMetadataView,
    OAuthServerMetadataView,
)

from .views import ConsentView, OAuthRevokeView, OAuthTokenView, RegisterView

app_name = "oauth2_provider"
urlpatterns = [
    path("mcp/oauth/authorize/", ConsentView.as_view(), name="authorize"),
    path("mcp/oauth/token/", OAuthTokenView.as_view(), name="token"),
    path("mcp/oauth/revoke/", OAuthRevokeView.as_view(), name="revoke-token"),
    path("mcp/oauth/register/", RegisterView.as_view(), name="dcr-register"),
    path(
        "mcp/oauth/register/<str:client_id>/",
        DynamicClientRegistrationManagementView.as_view(),
        name="dcr-register-management",
    ),
    path(
        ".well-known/oauth-authorization-server",
        OAuthServerMetadataView.as_view(),
        name="oauth-server-metadata",
    ),
    path(
        ".well-known/oauth-protected-resource",
        OAuthProtectedResourceMetadataView.as_view(),
        name="oauth-resource-metadata",
    ),
    path(
        ".well-known/oauth-protected-resource/mcp",
        OAuthProtectedResourceMetadataView.as_view(),
    ),
]
