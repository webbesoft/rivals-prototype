from datetime import datetime, timezone as utc_timezone

from django import template
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.formats import date_format

register = template.Library()


@register.filter
def local_datetime(value, format_string="D j M Y, H:i T"):
    """Localize timestamps stored as ISO strings in shared report snapshots."""
    if isinstance(value, str):
        try:
            value = parse_datetime(value.removesuffix(" UTC"))
        except ValueError:
            return ""
    if not isinstance(value, datetime):
        return ""
    if timezone.is_naive(value):
        value = timezone.make_aware(value, utc_timezone.utc)
    return date_format(timezone.localtime(value), format_string)


@register.filter
def date_time(value):
    # Your filter logic here
    return value.strftime("%Y-%m-%d %H:%M:%S")
