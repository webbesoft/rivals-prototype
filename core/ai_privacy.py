"""Keep credential operations out of remote error and performance telemetry."""

from functools import wraps
from urllib.parse import urlsplit

import sentry_sdk


def sensitive_ai(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with sentry_sdk.isolation_scope() as scope:
            scope.set_tag("sensitive_ai", True)
            return function(*args, **kwargs)

    return wrapped


def filter_ai_event(event, hint):
    path = urlsplit(event.get("request", {}).get("url", "")).path
    if event.get("tags", {}).get("sensitive_ai") or path.startswith(
        ("/accounts/gemini/", "/dashboard/decisions/ai/")
    ):
        return None
    return event


def filter_ai_breadcrumb(crumb, hint):
    url = str(crumb.get("data", {}).get("url", ""))
    if "generativelanguage.googleapis.com" in url:
        return None
    return crumb
