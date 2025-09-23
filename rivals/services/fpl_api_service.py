import logging

import requests

logger = logging.getLogger(__name__)


class FplApiService:
    BASE_URL = "https://fantasy.premierleague.com/api"
    HEADERS = {"User-Agent": "RivalSpy"}
    TIMEOUT = 30

    def make_request(self, url: str) -> dict | None:
        try:
            response = requests.get(url, headers=self.HEADERS, timeout=self.TIMEOUT)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"FPL API Exception: {e}")
            return None

    def fetch_mini_league(self, league_id: str) -> dict | None:
        url = f"{self.BASE_URL}/leagues-classic/{league_id}/standings/"
        response = self.make_request(url)
        if not response:
            return None
        return {
            "league_info": response["league"],
            "standings": response["standings"]["results"],
        }

    def fetch_team_history(self, team_id: str) -> dict | None:
        return self.make_request(f"{self.BASE_URL}/entry/{team_id}/history/")

    def fetch_team_transfers(self, team_id: str) -> dict | None:
        return self.make_request(f"{self.BASE_URL}/entry/{team_id}/transfers/")

    def fetch_team_picks(self, team_id: str, gameweek: int) -> dict | None:
        return self.make_request(
            f"{self.BASE_URL}/entry/{team_id}/event/{gameweek}/picks/"
        )

    def fetch_current_gameweek(self) -> int:
        response = self.make_request(f"{self.BASE_URL}/bootstrap-static/")
        if not response:
            return 1
        current_event = next((e for e in response["events"] if e["is_current"]), None)
        return current_event["id"] if current_event else 1

    def fetch_team_basic_info(self, team_id: int) -> dict | None:
        return self.make_request(f"{self.BASE_URL}/entry/{team_id}/")

    def fetch_elements(self) -> list | None:
        response = self.make_request(f"{self.BASE_URL}/bootstrap-static/")
        return response["elements"] if response else None

    def fetch_fixtures_data(self):
        """Fetch fixtures data"""
        fixtures = self.make_request(f"{self.BASE_URL}/fixtures/")
        if not fixtures:
            return None
        return fixtures
