import logging
from collections import defaultdict
from decimal import Decimal, InvalidOperation
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

    def get_best_xi(self) -> Optional[Dict]:
        """Maximise next-gameweek expected points with legal formations and club caps.

        Use FPL ep_next projections from one bootstrap snapshot. This is an
        unrestricted-budget starting XI, not a complete 15-player FPL squad.
        """
        cache_key = "landing_best_xi_expected_v2"
        cached = cache.get(cache_key)
        if cached is not None:
            return None if cached is False else cached

        bootstrap = self.fpl_api.fetch_bootstrap_static()
        if not bootstrap:
            return None
        clubs = {team["id"]: team for team in bootstrap.get("teams", [])}
        by_club = defaultdict(lambda: defaultdict(list))
        players = {}
        for player in bootstrap.get("elements", []):
            position = player.get("element_type")
            if position not in (1, 2, 3, 4) or player.get("team") not in clubs:
                continue
            try:
                expected_points = Decimal(str(player.get("ep_next")))
            except InvalidOperation:
                continue
            if not expected_points.is_finite():
                continue
            player = {**player, "expected_points": expected_points}
            players[player["id"]] = player
            by_club[player["team"]][position].append(player)

        # Dynamic programming keeps the best score for each positional count.
        # The temporary club count enforces the cap without greedy selection.
        limits = (1, 5, 5, 3)
        states = {(0, 0, 0, 0): (0, ())}
        for club_id in sorted(by_club):
            club_states = {(shape, 0): result for shape, result in states.items()}
            for position, candidates in sorted(by_club[club_id].items()):
                # At most three players from any club can feature, so lower
                # scorers at the same club and position cannot improve the XI.
                candidates.sort(key=lambda p: (-p["expected_points"], p["id"]))
                for player in candidates[:3]:
                    updated = dict(club_states)
                    for (shape, club_count), (points, ids) in club_states.items():
                        if (
                            club_count == 3
                            or shape[position - 1] == limits[position - 1]
                            or sum(shape) == 11
                        ):
                            continue
                        new_shape = list(shape)
                        new_shape[position - 1] += 1
                        key = (tuple(new_shape), club_count + 1)
                        result = (
                            points + player["expected_points"],
                            ids + (player["id"],),
                        )
                        if key not in updated or result[0] > updated[key][0]:
                            updated[key] = result
                    club_states = updated
            states = {}
            for (shape, _), result in club_states.items():
                if shape not in states or result[0] > states[shape][0]:
                    states[shape] = result

        legal = [
            (shape, result)
            for shape, result in states.items()
            if shape[0] == 1
            and sum(shape) == 11
            and shape[1] >= 3
            and shape[2] >= 2
            and shape[3] >= 1
        ]
        if not legal:
            return None
        shape, (points, ids) = max(legal, key=lambda item: item[1][0])
        if points <= 0:
            cache.set(cache_key, False, self.cache_timeout)
            return None
        selected = [players[player_id] for player_id in ids]
        rows = []
        for position, label in (
            (1, "Goalkeeper"),
            (2, "Defenders"),
            (3, "Midfielders"),
            (4, "Forwards"),
        ):
            rows.append(
                {
                    "label": label,
                    "players": [
                        {
                            "id": p["id"],
                            "name": p["web_name"],
                            "team": clubs[p["team"]].get("short_name", "UNK"),
                            "expected_points": p["expected_points"],
                            "cost": p.get("now_cost", 0) / 10,
                        }
                        for p in sorted(
                            selected, key=lambda p: (-p["expected_points"], p["id"])
                        )
                        if p["element_type"] == position
                    ],
                }
            )
        result = {
            "formation": "–".join(str(count) for count in shape[1:]),
            "expected_points": points,
            "cost": sum(p.get("now_cost", 0) for p in selected) / 10,
            "rows": rows,
        }
        cache.set(cache_key, result, self.cache_timeout)
        return result

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

        # Filter for upcoming fixtures: not started, not finished, and with a scheduled event
        upcoming = [
            f
            for f in fixtures
            if not f.get("started") and not f.get("finished") and f.get("event")
        ]
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

            team_matchups.append(
                {
                    "team": home_team_name,
                    "opponent": away_team_short,
                    "difficulty": h_diff,
                }
            )
            team_matchups.append(
                {
                    "team": away_team_name,
                    "opponent": home_team_short,
                    "difficulty": a_diff,
                }
            )

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
