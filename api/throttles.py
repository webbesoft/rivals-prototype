from collections.abc import Mapping

from rest_framework.throttling import BaseThrottle

from core.rate_limits import reserve_request


class RequestThrottle(BaseThrottle):
    def allow_request(self, request, view):
        action = getattr(view, "rate_limit_action", None)
        self.retry_after = 0
        if action and request.method in getattr(view, "rate_limit_methods", ("POST",)):
            self.retry_after = reserve_request(
                request,
                action,
                identity=(
                    request.data.get("email", "")
                    if isinstance(request.data, Mapping)
                    else ""
                ),
            )
        return not self.retry_after

    def wait(self):
        return self.retry_after
