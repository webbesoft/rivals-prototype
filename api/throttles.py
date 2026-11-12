from collections.abc import Mapping

from rest_framework.throttling import BaseThrottle

from core.rate_limits import POLICIES, reserve_request


class RequestThrottle(BaseThrottle):
    def allow_request(self, request, view):
        handler = getattr(view, request.method.lower(), None)
        action = getattr(
            view, "rate_limit_action", getattr(handler, "rate_limit_action", None)
        )
        methods = getattr(
            view,
            "rate_limit_methods",
            getattr(handler, "rate_limit_methods", ("POST",)),
        )
        self.retry_after = 0
        if action and request.method in methods:
            self.retry_after = reserve_request(
                request,
                action,
                identity=(
                    request.data.get("email", "")
                    if any(subject == "identity" for subject, _, _ in POLICIES[action])
                    and isinstance(request.data, Mapping)
                    else ""
                ),
            )
        return not self.retry_after

    def wait(self):
        return self.retry_after
