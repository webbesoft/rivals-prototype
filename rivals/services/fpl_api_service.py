import logging
import re
import time

import requests
from django.core.cache import cache

logger = logging.getLogger(__name__)


class FplApiService:
    BASE_URL = "https://fantasy.premierleague.com/api"
    HEADERS = {"User-Agent": "RivalSpy"}
    TIMEOUT = 30
    MAX_ATTEMPTS = 3
    RETRY_BACKOFF = 1
    RETRY_STATUS_CODES = {500, 502, 503, 504}
    BOOTSTRAP_CACHE_KEY = "fpl_bootstrap_static"
    BOOTSTRAP_CACHE_TIMEOUT = 300  # 5 minutes
    SEASON_CACHE_KEY = "fpl_current_season"
    SEASON_CACHE_TIMEOUT = 3600  # 1 hour
    FIXTURES_CACHE_KEY = "fpl_fixtures_data"
    FIXTURES_CACHE_TIMEOUT = 300  # 5 minutes

    def __init__(self, *, snapshot_only=False):
        self.snapshot_only = snapshot_only
        self.snapshots = {}

    def _snapshot_key(self, url):
        from .gameweek_context_service import GameweekContextService

        if url == f"{self.BASE_URL}/bootstrap-static/":
            return "bootstrap"
        if url == f"{self.BASE_URL}/fixtures/":
            return "fixtures"
        bootstrap = getattr(self, "_global_snapshots", {}).get("bootstrap")
        season = (
            GameweekContextService.derive(bootstrap.get("events", []))["season"]
            if self.snapshot_only and bootstrap
            else GameweekContextService.cached()["season"]
        )
        entry = re.fullmatch(
            re.escape(self.BASE_URL) + r"/entry/(\d+)/(history/)?", url
        )
        if entry and season:
            return f"{'history' if entry[2] else 'profile'}:{season}:{entry[1]}"
        picks = re.fullmatch(
            re.escape(self.BASE_URL) + r"/entry/(\d+)/event/(\d+)/picks/", url
        )
        if picks and season:
            return f"picks:{season}:{picks[1]}:{picks[2]}"
        live = re.fullmatch(re.escape(self.BASE_URL) + r"/event/(\d+)/live/", url)
        return f"live:{season}:{live[1]}" if live and season else None

    def _read_snapshot(self, key):
        from .fpl_snapshot_service import read_snapshot, request_snapshot

        if key in ("bootstrap", "fixtures"):
            if not hasattr(self, "_global_snapshots"):
                from django.utils import timezone

                from rivals.models import FplSnapshot

                self._global_snapshots = dict(
                    FplSnapshot.objects.filter(
                        key__in=["bootstrap", "fixtures"], expires_at__gt=timezone.now()
                    ).values_list("key", "payload")
                )
            data = self._global_snapshots.get(key)
        else:
            data = read_snapshot(key)
        if data is None:
            request_snapshot(key)
        return data

    def make_request(self, url: str) -> dict | None:
        if self.snapshot_only:
            key = self._snapshot_key(url)
            return self._read_snapshot(key) if key else None
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                response = requests.get(url, headers=self.HEADERS, timeout=self.TIMEOUT)
                response.raise_for_status()
                data = response.json()
                # Publication happens only after the worker rechecks its lease.
                if url.endswith(
                    (
                        "/bootstrap-static/",
                        "/fixtures/",
                        "/picks/",
                        "/live/",
                        "/history/",
                    )
                ) or re.fullmatch(re.escape(self.BASE_URL) + r"/entry/\d+/", url):
                    key = self._snapshot_key(url)
                    if key:
                        self.snapshots[key] = data
                return data
            except requests.RequestException as exc:
                retryable = isinstance(
                    exc, (requests.ConnectionError, requests.Timeout)
                ) or (
                    isinstance(exc, requests.HTTPError)
                    and exc.response is not None
                    and exc.response.status_code in self.RETRY_STATUS_CODES
                )
                if not retryable or attempt == self.MAX_ATTEMPTS:
                    logger.error("FPL API Exception: %s", exc)
                    return None
                delay = self.RETRY_BACKOFF * 2 ** (attempt - 1)
                logger.warning(
                    "FPL API request failed for %s (attempt %s/%s); "
                    "retrying in %ss: %s",
                    url,
                    attempt,
                    self.MAX_ATTEMPTS,
                    delay,
                    exc,
                )
                time.sleep(delay)

    def fetch_bootstrap_static(self, force_refresh: bool = False) -> dict | None:
        """Fetch bootstrap-static with caching to prevent duplicate high-volume calls."""
        if self.snapshot_only:
            return self._read_snapshot("bootstrap")
        if not force_refresh:
            cached = cache.get(self.BOOTSTRAP_CACHE_KEY)
            if cached:
                return cached

        data = self.make_request(f"{self.BASE_URL}/bootstrap-static/")
        if data:
            cache.set(self.BOOTSTRAP_CACHE_KEY, data, self.BOOTSTRAP_CACHE_TIMEOUT)
        return data

    def fetch_mini_league(self, league_id: str, max_pages: int = 10) -> dict | None:
        url = f"{self.BASE_URL}/leagues-classic/{league_id}/standings/"
        response = self.make_request(url)
        if not response:
            return None

        standings_data = response.get("standings", {})
        results = list(standings_data.get("results", []))
        has_next = standings_data.get("has_next", False)
        is_complete = not has_next
        incomplete_reason = None

        page = 2
        while has_next and page <= max_pages:
            next_resp = self.make_request(f"{url}?page_standings={page}")
            if not next_resp or "standings" not in next_resp:
                is_complete = False
                incomplete_reason = f"Standings page {page} unavailable"
                break
            page_standings = next_resp.get("standings", {})
            results.extend(page_standings.get("results", []))
            has_next = page_standings.get("has_next", False)
            if not has_next:
                is_complete = True
            page += 1

        if has_next and page > max_pages:
            is_complete = False
            incomplete_reason = (
                f"Standings exceed the {max_pages}-page import limit "
                f"({len(results)} entries fetched)"
            )

        return {
            "league_info": response["league"],
            "standings": results,
            "is_complete": is_complete,
            "incomplete_reason": incomplete_reason,
        }

    def fetch_mini_league_page(self, league_id, page):
        return self.make_request(
            f"{self.BASE_URL}/leagues-classic/{league_id}/standings/"
            f"?page_standings={page}"
        )

    def fetch_team_history(self, team_id: str) -> dict | None:
        return self.make_request(f"{self.BASE_URL}/entry/{team_id}/history/")

    def fetch_team_transfers(self, team_id: str) -> dict | None:
        return self.make_request(f"{self.BASE_URL}/entry/{team_id}/transfers/")

    def fetch_team_picks(self, team_id: str, gameweek: int) -> dict | None:
        return self.make_request(
            f"{self.BASE_URL}/entry/{team_id}/event/{gameweek}/picks/"
        )

    def fetch_gameweek_live(self, gameweek, season):
        if self.snapshot_only:
            return self._read_snapshot(f"live:{season}:{gameweek}")
        key = f"fpl_live_{season}_{gameweek}"
        cached = cache.get(key)
        if cached is not None:
            return cached
        data = self.make_request(f"{self.BASE_URL}/event/{gameweek}/live/")
        if data is not None:
            cache.set(key, data, 120)
        return data

    def fetch_current_gameweek(self) -> int | None:
        response = self.fetch_bootstrap_static()
        if not response or not response.get("events"):
            return None
        from rivals.services.gameweek_context_service import GameweekContextService

        return GameweekContextService.derive(response["events"])["scoring_gameweek"]

    def fetch_current_season(self, force_refresh: bool = False) -> str | None:
        """
        Derive the current FPL season label (e.g. "2025/26") from bootstrap-static.
        The season is built from the deadline year of the first event.
        Caches aggressively to avoid live HTTP calls on every page load.
        """
        if not force_refresh and not self.snapshot_only:
            cached_season = cache.get(self.SEASON_CACHE_KEY)
            if cached_season:
                return cached_season

        response = self.fetch_bootstrap_static(force_refresh=force_refresh)
        if not response or not response.get("events"):
            return None
        try:
            first_event = response["events"][0]
            deadline = first_event["deadline_time"]  # e.g. "2025-08-15T17:30:00Z"
            start_year = int(deadline[:4])  # e.g. 2025
            end_year = (start_year + 1) % 100  # e.g. 26
            season_str = f"{start_year}/{end_year:02d}"  # e.g. "2025/26"
            cache.set(self.SEASON_CACHE_KEY, season_str, self.SEASON_CACHE_TIMEOUT)
            return season_str
        except (KeyError, ValueError, IndexError) as e:
            logger.error(f"Could not determine current season: {e}")
            return None

    def fetch_team_basic_info(self, team_id: int) -> dict | None:
        return self.make_request(f"{self.BASE_URL}/entry/{team_id}/")

    def fetch_elements(self) -> list | None:
        response = self.fetch_bootstrap_static()
        return response.get("elements") if response else None

    def fetch_teams(self) -> list | None:
        response = self.fetch_bootstrap_static()
        return response.get("teams") if response else None

    def fetch_fixtures_data(self, force_refresh: bool = False):
        """Fetch fixtures data with caching."""
        if self.snapshot_only:
            return self._read_snapshot("fixtures")
        if not force_refresh:
            cached = cache.get(self.FIXTURES_CACHE_KEY)
            if cached:
                return cached

        fixtures = self.make_request(f"{self.BASE_URL}/fixtures/")
        if fixtures:
            cache.set(self.FIXTURES_CACHE_KEY, fixtures, self.FIXTURES_CACHE_TIMEOUT)
        return fixtures

    def get_latest_passed_deadline(self):
        response = self.fetch_bootstrap_static()
        if not response:
            return None, None
        from rivals.services.gameweek_context_service import GameweekContextService

        context = GameweekContextService.derive(response.get("events", []))
        return context["scoring_gameweek"], context["latest_deadline"]
