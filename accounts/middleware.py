from urllib.parse import urlencode

from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.deprecation import MiddlewareMixin
from django.utils.cache import add_never_cache_headers

from .onboarding import has_fpl_setup


class OnboardingMiddleware(MiddlewareMixin):
    def process_response(self, request, response):
        if request.path_info.startswith(("/accounts/", "/api/auth/", "/api/me/")):
            add_never_cache_headers(response)
            # no-referrer makes browser form POSTs send Origin: null, failing CSRF.
            # Preserve explicit policies on redirects containing sign-in secrets.
            response.setdefault("Referrer-Policy", "same-origin")
        return response

    def process_view(self, request, view_func, view_args, view_kwargs):
        if not settings.PASSWORDLESS_AUTH_ENABLED:
            return None
        match = request.resolver_match
        # DRF must authenticate bearer tokens before applying its own gate.
        protected = (
            match.namespace == "rivals"
            or (
                match.namespace == "accounts"
                and match.url_name
                in {
                    "profile",
                    "update_fpl_id",
                    "gemini",
                    "digests",
                    "digests.detail",
                    "mcp",
                    "connections",
                }
            )
            or (match.namespace == "oauth2_provider" and match.url_name == "authorize")
        )
        if not protected or not request.user.is_authenticated:
            return None
        if not request.user.is_active or not request.user.is_verified:
            return redirect("accounts:login")
        if not has_fpl_setup(request.user):
            url = reverse("accounts:onboarding")
            response = redirect(f"{url}?{urlencode({'next': request.get_full_path()})}")
            if request.headers.get("HX-Request") == "true":
                response.status_code = 200
                response["HX-Redirect"] = response.url
            return response
        return None
