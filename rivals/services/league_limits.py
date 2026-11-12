"""Hard ceilings apply even when deployment configuration is overly permissive."""

from django.conf import settings

MAX_LEAGUE_ROWS = 1000
MAX_PAGE_ROWS = 100
MAX_IMPORT_PAGES = 100


def full_import_limit():
    return max(1, min(MAX_LEAGUE_ROWS, settings.LEAGUE_FULL_IMPORT_LIMIT))
