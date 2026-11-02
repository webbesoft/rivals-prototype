import logging
from typing import Dict, List, Optional

from django.core.cache import cache

from .fpl_api_service import FplApiService

logger = logging.getLogger(__name__)


class LandingDataService:
    """Service to aggregate public FPL data for the landing page."""

    def __init__(self):
        self.fpl_api = FplApiService()
        self.cache_timeout = 900  # 15 minutes

    def get_current_gameweek(self) -> int:
        """Get the current active gameweek."""
        cache_key = "landing_current_gameweek"
        cached = cache.get(cache_key)
        if cached:
            return cached

        gameweek = self.fpl_api.fetch_current_gameweek()
        cache.set(cache_key, gameweek, self.cache_timeout)
        return gameweek

    def get_top_captain_picks(self, limit: int = 5) -> List[Dict]:
        """Get the most captained players this gameweek."""
        cache_key = f"landing_captain_picks_{limit}"
        cached = cache.get(cache_key)
        if cached:
            return cached

        elements = self.fpl_api.fetch_elements()
        if not elements:
            return []

        # Sort by selected_by_percent (captaincy proxy)
        sorted_players = sorted(
            elements,
            key=lambda x: float(x.get("selected_by_percent", 0)),
            reverse=True,
        )[:limit]

        captain_picks = [
            {
                "name": player["web_name"],
                "team": self._get_team_short_name(player.get("team")),
                "captaincy_percent": player.get("selected_by_percent", "0"),
                "form": player.get("form", "0"),
                "expected_points": player.get("ep_next", "0"),
            }
            for player in sorted_players
        ]

        cache.set(cache_key, captain_picks, self.cache_timeout)
        return captain_picks

    def get_transfer_trends(self, limit: int = 5) -> Dict[str, List[Dict]]:
        """Get the most transferred in/out players."""
        cache_key = f"landing_transfer_trends_{limit}"
        cached = cache.get(cache_key)
        if cached:
            return cached

        elements = self.fpl_api.fetch_elements()
        if not elements:
            return {"transfers_in": [], "transfers_out": []}

        # Sort by transfers_in_event
        sorted_in = sorted(
            elements,
            key=lambda x: int(x.get("transfers_in_event", 0)),
            reverse=True,
        )[:limit]

        # Sort by transfers_out_event
        sorted_out = sorted(
            elements,
            key=lambda x: int(x.get("transfers_out_event", 0)),
            reverse=True,
        )[:limit]

        trends = {
            "transfers_in": [
                {
                    "name": player["web_name"],
                    "team": self._get_team_short_name(player.get("team")),
                    "transfers_in_delta": player.get("transfers_in_event", 0),
                    "cost": player.get("now_cost", 0) / 10,
                }
                for player in sorted_in
            ],
            "transfers_out": [
                {
                    "name": player["web_name"],
                    "team": self._get_team_short_name(player.get("team")),
                    "transfers_out_delta": player.get("transfers_out_event", 0),
                    "cost": player.get("now_cost", 0) / 10,
                }
                for player in sorted_out
            ],
        }

        cache.set(cache_key, trends, self.cache_timeout)
        return trends

    def get_fixture_difficulty_highlights(self) -> Dict[str, List[Dict]]:
        """Get best and worst upcoming fixtures dynamically from live FPL fixture data."""
        cache_key = "landing_fixture_highlights"
        cached = cache.get(cache_key)
        if cached:
            return cached

        fixtures = self.fpl_api.fetch_fixtures_data()
        teams_map = self._get_teams_map()

        if not fixtures or not teams_map:
            return {"best_fixtures": [], "worst_fixtures": []}

        # Filter for upcoming unfinished fixtures that have an event scheduled
        upcoming = [f for f in fixtures if not f.get("finished") and f.get("event")]
        if not upcoming:
            return {"best_fixtures": [], "worst_fixtures": []}

        # Find the next upcoming gameweek event id
        next_event = min(f["event"] for f in upcoming)
        next_gw_fixtures = [f for f in upcoming if f["event"] == next_event]

        team_matchups = []
        for fix in next_gw_fixtures:
            home_id = fix.get("team_h")
            away_id = fix.get("team_a")
            h_diff = fix.get("team_h_difficulty", 3)
            a_diff = fix.get("team_a_difficulty", 3)

            home_team_name = self._get_team_name(home_id)
            away_team_short = self._get_team_short_name(away_id)
            away_team_name = self._get_team_name(away_id)
            home_team_short = self._get_team_short_name(home_id)

            team_matchups.append({
                "team": home_team_name,
                "opponent": away_team_short,
                "difficulty": h_diff,
            })
            team_matchups.append({
                "team": away_team_name,
                "opponent": home_team_short,
                "difficulty": a_diff,
            })

        best = sorted(team_matchups, key=lambda x: (x["difficulty"], x["team"]))[:3]
        worst = sorted(team_matchups, key=lambda x: (-x["difficulty"], x["team"]))[:3]

        highlights = {
            "best_fixtures": best,
            "worst_fixtures": worst,
        }

        cache.set(cache_key, highlights, self.cache_timeout)
        return highlights

    def get_demo_team_data(self, team_id: Optional[int] = None) -> Optional[Dict]:
        """Get full stats for a showcase team."""
        # Use a popular team as demo (e.g., FPL Focal's team or a top-ranked team)
        demo_team_id = team_id or 2833701  # Example: popular FPL content creator

        cache_key = f"landing_demo_team_{demo_team_id}"
        cached = cache.get(cache_key)
        if cached:
            return cached

        team_info = self.fpl_api.fetch_team_basic_info(demo_team_id)
        team_history = self.fpl_api.fetch_team_history(str(demo_team_id))

        if not team_info or not team_history:
            return None

        demo_data = {
            "team_name": team_info.get("name"),
            "manager_name": team_info.get("player_first_name", "")
            + " "
            + team_info.get("player_last_name", ""),
            "overall_rank": team_info.get("summary_overall_rank"),
            "total_points": team_info.get("summary_overall_points"),
            "gameweek_points": team_info.get("summary_event_points"),
            "value": team_info.get("last_deadline_value", 1000) / 10,
            "bank": team_info.get("last_deadline_bank", 0) / 10,
            "recent_performance": team_history.get("current", [])[-5:],
        }

        cache.set(cache_key, demo_data, self.cache_timeout)
        return demo_data

    def _get_teams_map(self) -> Dict[int, Dict]:
        """Cache and return mapping of team ID to team dictionary."""
        cache_key = "landing_teams_map"
        cached = cache.get(cache_key)
        if cached:
            return cached

        teams = self.fpl_api.fetch_teams()
        if not teams:
            return {}

        teams_map = {t["id"]: t for t in teams}
        cache.set(cache_key, teams_map, 3600)  # 1 hour TTL
        return teams_map

    def _get_team_short_name(self, team_id: Optional[int]) -> str:
        """Get team short name from ID dynamically from bootstrap data."""
        if not team_id:
            return "UNK"
        teams_map = self._get_teams_map()
        if team_id in teams_map:
            return teams_map[team_id].get("short_name", "UNK")
        return "UNK"

    def _get_team_name(self, team_id: Optional[int]) -> str:
        """Get team display name from ID dynamically from bootstrap data."""
        if not team_id:
            return "Unknown"
        teams_map = self._get_teams_map()
        if team_id in teams_map:
            return teams_map[team_id].get("name", "Unknown")
        return "Unknown"
