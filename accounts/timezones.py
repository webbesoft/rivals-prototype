"""Validated account timezones, with UTC until a preference is saved."""

from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from django.conf import settings
from django.core.exceptions import ValidationError


@lru_cache(maxsize=1)
def timezone_names():
    return frozenset(available_timezones() - {"localtime", "posixrules"})


def validate_timezone(value):
    if value not in timezone_names():
        raise ValidationError("Choose a valid timezone.", code="invalid_timezone")


def user_timezone(user):
    name = getattr(user, "timezone", "") or settings.TIME_ZONE
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(settings.TIME_ZONE)


def timezone_choices():
    return [("UTC", "UTC — Coordinated Universal Time")] + [
        (name, name.replace("_", " ")) for name in sorted(timezone_names() - {"UTC"})
    ]
