# rivals/services/fpl_analysis.py
import logging
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from ..models import Team
from .fpl_api_service import FplApiService

logger = logging.getLogger(__name__)


class TeamAnalysisService:
    """
    Synchronous analysis service that uses the existing FplApiService (requests-based).
    Methods:
      - refresh_data()  -> preload bootstrap & fixtures
      - analyze_team(fpl_team_id, include_team_obj=None) -> Dict analysis result
      - compare_teams(my_fpl_team_id, rival_fpl_team_id, include_models=None) -> Dict comparison
    """

    def __init__(self, api: Optional[FplApiService] = None):
        self.api = api or FplApiService()
        self.bootstrap: Optional[Dict[str, Any]] = None
        self.elements: List[Dict[str, Any]] = []
        self.teams: Dict[int, Dict[str, Any]] = {}
        self.events: List[Dict[str, Any]] = []
        self.fixtures: List[Dict[str, Any]] = []
        self.current_gw: int = 1

    # ---------- Data loading / caching ----------
    def refresh_data(self) -> bool:
        """
        Fetch bootstrap-static and fixtures. Returns True if success.
        Call this from a management command or a periodic task if desired.
        """
        try:
            bootstrap = self.api.make_request(f"{self.api.BASE_URL}/bootstrap-static/")
            if not bootstrap:
                logger.error("FPL bootstrap-static fetch returned None")
                return False

            self.bootstrap = bootstrap
            self.elements = bootstrap.get("elements", [])
            self.teams = {t["id"]: t for t in bootstrap.get("teams", [])}
            self.events = bootstrap.get("events", [])
            current_event = next((e for e in self.events if e.get("is_current")), None)
            self.current_gw = current_event["id"] if current_event else 1

            fixtures = self.api.fetch_fixtures_data()
            self.fixtures = fixtures or []
            logger.info(
                "FPL data refreshed: %d players, %d fixtures",
                len(self.elements),
                len(self.fixtures),
            )
            return True
        except Exception as e:
            logger.exception("Failed to refresh FPL data: %s", e)
            return False

    def ensure_data(self) -> None:
        """Ensure bootstrap & fixtures are loaded (raises RuntimeError on failure)."""
        if self.bootstrap is None or not self.fixtures:
            ok = self.refresh_data()
            if not ok:
                raise RuntimeError("Unable to load FPL bootstrap/fixtures data")

    # ---------- Utility helpers ----------
    def _safe_float(self, value, default=0.0) -> float:
        try:
            return float(value) if value is not None else default
        except Exception:
            return default

    def get_position_name(self, position_id: int) -> str:
        return {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}.get(position_id, "UNK")

    # ---------- Core analytics ----------
    def calculate_fixture_difficulty(
        self, team_id: int, next_fixtures: int = 5
    ) -> float:
        """Weighted average fixture difficulty for next N fixtures (home advantage applied)."""
        fixtures = [f for f in self.fixtures if not f.get("finished")]
        team_fixtures = []
        for fx in fixtures:
            event = fx.get("event")
            if fx.get("team_h") == team_id:
                team_fixtures.append(
                    {
                        "difficulty": fx.get("team_h_difficulty", 3),
                        "is_home": True,
                        "gameweek": event,
                    }
                )
            elif fx.get("team_a") == team_id:
                team_fixtures.append(
                    {
                        "difficulty": fx.get("team_a_difficulty", 3),
                        "is_home": False,
                        "gameweek": event,
                    }
                )

        team_fixtures = sorted(team_fixtures, key=lambda x: x["gameweek"])[
            :next_fixtures
        ]
        if not team_fixtures:
            return 3.0

        weighted = []
        for f in team_fixtures:
            d = f["difficulty"]
            if f["is_home"]:
                d = max(1.0, d - 0.2)
            weighted.append(max(1.0, min(5.0, d)))
        return sum(weighted) / len(weighted)

    def calculate_expected_points(
        self, player: Dict[str, Any], fixture_difficulty: float
    ) -> float:
        """Estimate expected points over 5 gameweeks based on form & PPG and fixtures"""
        form = self._safe_float(player.get("form", 0))
        ppg = self._safe_float(player.get("points_per_game", 0))
        base = form * 0.6 + ppg * 0.4
        multiplier = 1.0
        if fixture_difficulty <= 2.5:
            multiplier = 1.2
        elif fixture_difficulty >= 4.0:
            multiplier = 0.8
        return base * multiplier * 5

    def get_top_alternatives_by_position(
        self, position_id: int, exclude_ids: List[int], limit: int = 5
    ) -> List[Dict[str, Any]]:
        """Return a list of candidate players (dicts) for a given position"""
        players = [
            p
            for p in self.elements
            if p.get("element_type") == position_id
            and p.get("id") not in set(exclude_ids)
            and (p.get("minutes", 0) > 0)
        ]
        candidates = []
        for p in players:
            team_id = p.get("team")
            fixture_difficulty = self.calculate_fixture_difficulty(
                team_id, next_fixtures=5
            )
            expected = self.calculate_expected_points(p, fixture_difficulty)
            now_cost = p.get("now_cost") or 0
            value_rating = (
                (float(p.get("total_points", 0)) / float(now_cost or 1)) * 10
                if now_cost
                else 0.0
            )
            candidates.append(
                {
                    "id": p["id"],
                    "name": f"{p.get('first_name','')} {p.get('second_name','')}".strip(),
                    "team": self.teams.get(team_id, {}).get("short_name"),
                    "position": self.get_position_name(p.get("element_type")),
                    "total_points": p.get("total_points", 0),
                    "form": self._safe_float(p.get("form", 0)),
                    "points_per_game": self._safe_float(p.get("points_per_game", 0)),
                    "price": float(now_cost) / 10.0,
                    "price_change": float(p.get("cost_change_event", 0)) / 10.0,
                    "ownership": self._safe_float(p.get("selected_by_percent", 0)),
                    "fixture_difficulty_next_5": fixture_difficulty,
                    "expected_points_next_5": expected,
                    "value_rating": value_rating,
                    "raw": p,  # keep raw for debug if needed
                }
            )

        candidates.sort(key=lambda x: x["expected_points_next_5"], reverse=True)
        return candidates[:limit]

    def analyze_transfer_priorities(
        self, current_squad: List[Dict[str, Any]], top_k: int = 5
    ) -> List[Dict[str, Any]]:
        """
        Examine each player in the current squad and propose better alternatives,
        returning a list of recommendation dicts.
        """
        bootstrap = self.bootstrap or {}
        if not bootstrap:
            raise RuntimeError("Bootstrap data required")
        current_player_ids = [pick["element"] for pick in current_squad]

        recommendations = []
        position_groups = defaultdict(list)
        for pick in current_squad:
            player_id = pick["element"]
            player_obj = next((p for p in self.elements if p["id"] == player_id), None)
            if not player_obj:
                continue
            position_groups[player_obj["element_type"]].append(
                {"pick": pick, "player": player_obj}
            )

        for position_id, players in position_groups.items():
            for pinfo in players:
                player = pinfo["player"]
                fixture_difficulty = self.calculate_fixture_difficulty(
                    player["team"], 5
                )
                expected_current = self.calculate_expected_points(
                    player, fixture_difficulty
                )

                alternatives = self.get_top_alternatives_by_position(
                    position_id, current_player_ids, limit=10
                )
                # pick alts that beat current by a margin (tunable)
                significant_alts = [
                    alt
                    for alt in alternatives
                    if alt["expected_points_next_5"] > expected_current + 6.0
                ]

                if not significant_alts:
                    continue

                best_alt = significant_alts[0]
                points_gap = best_alt["expected_points_next_5"] - expected_current
                fixture_advantage = (
                    fixture_difficulty - best_alt["fixture_difficulty_next_5"]
                )
                priority_score = points_gap + (fixture_advantage * 3)

                rec = {
                    "transfer_out": {
                        "id": player["id"],
                        "name": f"{player.get('first_name','')} {player.get('second_name','')}".strip(),
                        "team": self.teams.get(player["team"], {}).get("short_name"),
                        "position": self.get_position_name(player["element_type"]),
                        "total_points": player.get("total_points", 0),
                        "form": self._safe_float(player.get("form", 0)),
                        "points_per_game": self._safe_float(
                            player.get("points_per_game", 0)
                        ),
                        "price": float(player.get("now_cost", 0)) / 10.0,
                    },
                    "transfer_in": significant_alts[:3],
                    "priority_score": round(priority_score, 2),
                    "reasoning": self.generate_transfer_reasoning(
                        player, best_alt, fixture_difficulty
                    ),
                }
                recommendations.append(rec)

        recommendations.sort(key=lambda r: r["priority_score"], reverse=True)
        return recommendations[:top_k]

    def get_captain_suggestions(
        self, current_squad: List[Dict[str, Any]], limit: int = 5
    ) -> List[Dict[str, Any]]:
        candidates = []
        for pick in current_squad:
            player_id = pick.get("element")
            player = next((p for p in self.elements if p["id"] == player_id), None)
            if not player:
                continue
            if self._safe_float(player.get("form", 0)) < 3.0:
                continue
            fd = self.calculate_fixture_difficulty(player["team"], next_fixtures=1)
            expected = (
                self.calculate_expected_points(player, fd) / 5.0
            )  # single gw proxy
            candidates.append(
                {
                    "id": player["id"],
                    "name": f"{player.get('first_name','')} {player.get('second_name','')}".strip(),
                    "team": self.teams.get(player["team"], {}).get("short_name"),
                    "position": self.get_position_name(player["element_type"]),
                    "form": self._safe_float(player.get("form", 0)),
                    "points_per_game": self._safe_float(
                        player.get("points_per_game", 0)
                    ),
                    "price": float(player.get("now_cost", 0)) / 10.0,
                    "fixture_difficulty_next_5": fd,
                    "expected_points_next_gw": expected,
                }
            )
        candidates.sort(key=lambda x: x["expected_points_next_gw"], reverse=True)
        return candidates[:limit]

    def generate_transfer_reasoning(
        self,
        current_player: Dict[str, Any],
        alternative: Dict[str, Any],
        current_fixture_difficulty: float,
    ) -> str:
        reasons = []
        cur_form = self._safe_float(current_player.get("form", 0))
        alt_form = (
            self._safe_float(alternative.get("form", 0))
            if alternative.get("form") is not None
            else alternative.get("form", 0)
        )
        try:
            alt_form = float(alt_form)
        except Exception:
            alt_form = 0.0

        if alt_form > cur_form + 1:
            reasons.append(f"Better recent form ({alt_form:.1f} vs {cur_form:.1f})")
        if (
            alternative.get("fixture_difficulty_next_5", 3)
            < current_fixture_difficulty - 0.5
        ):
            reasons.append("Easier upcoming fixtures")
        if alternative.get("expected_points_next_5", 0) > 0:
            gap = alternative["expected_points_next_5"] - (cur_form * 5)
            reasons.append(f"Higher expected points (+{gap:.1f})")
        return "; ".join(reasons) if reasons else "Statistical upgrade recommended"

    # ---------- Squad-level analysis & comparisons ----------
    def analyze_squad_metrics(
        self, squad_picks: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Return aggregated metrics for the given squad picks."""
        players = []
        total_expected = 0.0
        total_fd = 0.0
        total_points = 0

        for pick in squad_picks:
            p = next((x for x in self.elements if x["id"] == pick["element"]), None)
            if not p:
                continue
            fd = self.calculate_fixture_difficulty(p["team"], 5)
            expected = self.calculate_expected_points(p, fd)
            players.append(p)
            total_expected += expected
            total_fd += fd
            total_points += p.get("total_points", 0)

        count = len(players) or 1
        return {
            "players": players,
            "total_expected_points": total_expected,
            "avg_fixture_difficulty": (total_fd / count) if players else 3.0,
            "avg_player_points": (total_points / count) if players else 0.0,
        }

    def create_team_summary(
        self,
        team_info: Dict[str, Any],
        team_picks: Dict[str, Any],
        squad_analysis: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Return a JSON-serializable team summary (no pydantic)."""
        players = squad_analysis.get("players", [])
        if players:
            top = max(players, key=lambda p: p.get("total_points", 0))
            top_performer = {
                "id": top["id"],
                "name": f"{top.get('first_name','')} {top.get('second_name','')}".strip(),
                "team": self.teams.get(top["team"], {}).get("short_name"),
                "total_points": top.get("total_points", 0),
                "price": float(top.get("now_cost", 0)) / 10.0,
            }
        else:
            top_performer = None

        return {
            "team_id": team_info.get("id"),
            "team_name": team_info.get("name"),
            "manager_name": f"{team_info.get('player_first_name','')} {team_info.get('player_last_name','')}".strip(),
            "total_points": team_info.get("summary_overall_points", 0),
            "gameweek_points": team_info.get("summary_event_points", 0),
            "overall_rank": team_info.get("summary_overall_rank", 0),
            "squad_value": float(team_picks.get("entry_history", {}).get("value", 0))
            / 10.0,
            "bank": float(team_picks.get("entry_history", {}).get("bank", 0)) / 10.0,
            "total_transfers": team_info.get("summary_total_transfers", 0),
            "average_player_points": squad_analysis.get("avg_player_points"),
            "top_performer": top_performer,
            "squad_expected_points_next_5": squad_analysis.get("total_expected_points"),
            "squad_fixture_difficulty": squad_analysis.get("avg_fixture_difficulty"),
        }

    def compare_positions(
        self, my_squad: List[Dict[str, Any]], rival_squad: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Return per-position comparisons similar to previous PositionComparison pydantic model."""
        my_positions = defaultdict(list)
        rival_positions = defaultdict(list)

        for pick in my_squad:
            p = next((x for x in self.elements if x["id"] == pick["element"]), None)
            if p:
                my_positions[p["element_type"]].append(p)

        for pick in rival_squad:
            p = next((x for x in self.elements if x["id"] == pick["element"]), None)
            if p:
                rival_positions[p["element_type"]].append(p)

        comparisons = []
        for pos_id in (1, 2, 3, 4):
            my_players = my_positions.get(pos_id, [])
            rival_players = rival_positions.get(pos_id, [])
            if not my_players or not rival_players:
                continue

            my_avg = sum(p.get("total_points", 0) for p in my_players) / len(my_players)
            their_avg = sum(p.get("total_points", 0) for p in rival_players) / len(
                rival_players
            )

            my_expected = sum(
                self.calculate_expected_points(
                    p, self.calculate_fixture_difficulty(p["team"], 5)
                )
                for p in my_players
            ) / len(my_players)
            their_expected = sum(
                self.calculate_expected_points(
                    p, self.calculate_fixture_difficulty(p["team"], 5)
                )
                for p in rival_players
            ) / len(rival_players)

            diff = my_avg - their_avg
            if abs(diff) < 5 and abs(my_expected - their_expected) < 5:
                adv = "equal"
            elif diff > 0 and (my_expected - their_expected) > 0:
                adv = "mine"
            elif diff < 0 and (my_expected - their_expected) < 0:
                adv = "theirs"
            else:
                adv = "mixed"

            comparisons.append(
                {
                    "position": self.get_position_name(pos_id),
                    "my_team_avg_points": my_avg,
                    "rival_team_avg_points": their_avg,
                    "my_team_expected": my_expected,
                    "rival_team_expected": their_expected,
                    "advantage": adv,
                    "difference": diff,
                }
            )
        return comparisons

    def generate_head_to_head_insights(
        self,
        my_summary: Dict[str, Any],
        rival_summary: Dict[str, Any],
        position_comparisons: List[Dict[str, Any]],
    ) -> Tuple[Dict[str, str], List[str]]:
        advantages = {
            "overall_rank": (
                "mine"
                if (
                    my_summary.get("overall_rank", 999999)
                    < rival_summary.get("overall_rank", 999999)
                )
                else "theirs"
            ),
            "total_points": (
                "mine"
                if my_summary.get("total_points", 0)
                > rival_summary.get("total_points", 0)
                else "theirs"
            ),
            "gameweek_points": (
                "mine"
                if my_summary.get("gameweek_points", 0)
                > rival_summary.get("gameweek_points", 0)
                else "theirs"
            ),
            "squad_value": (
                "mine"
                if my_summary.get("squad_value", 0)
                > rival_summary.get("squad_value", 0)
                else "theirs"
            ),
            "bank_balance": (
                "mine"
                if my_summary.get("bank", 0) > rival_summary.get("bank", 0)
                else "theirs"
            ),
            "upcoming_fixtures": (
                "mine"
                if my_summary.get("squad_fixture_difficulty", 999)
                < rival_summary.get("squad_fixture_difficulty", 999)
                else "theirs"
            ),
            "expected_points": (
                "mine"
                if my_summary.get("squad_expected_points_next_5", 0)
                > rival_summary.get("squad_expected_points_next_5", 0)
                else "theirs"
            ),
        }

        insights = []
        pts_gap = abs(
            my_summary.get("total_points", 0) - rival_summary.get("total_points", 0)
        )
        if pts_gap > 100:
            leader = (
                "You"
                if my_summary.get("total_points", 0)
                > rival_summary.get("total_points", 0)
                else "Your rival"
            )
            insights.append(
                f"{leader} have a significant {pts_gap} point lead this season"
            )

        gw_gap = abs(
            my_summary.get("gameweek_points", 0)
            - rival_summary.get("gameweek_points", 0)
        )
        if gw_gap > 20:
            better = (
                "You"
                if my_summary.get("gameweek_points", 0)
                > rival_summary.get("gameweek_points", 0)
                else "Your rival"
            )
            insights.append(
                f"{better} had the better gameweek ({max(my_summary.get('gameweek_points',0), rival_summary.get('gameweek_points',0))} pts)"
            )

        fixture_diff = abs(
            (my_summary.get("squad_fixture_difficulty", 0) or 0)
            - (rival_summary.get("squad_fixture_difficulty", 0) or 0)
        )
        if fixture_diff > 0.5:
            better = (
                "You"
                if (
                    my_summary.get("squad_fixture_difficulty", 999)
                    < rival_summary.get("squad_fixture_difficulty", 999)
                )
                else "Your rival"
            )
            insights.append(f"{better} have significantly easier upcoming fixtures")

        my_adv = sum(1 for pc in position_comparisons if pc.get("advantage") == "mine")
        their_adv = sum(
            1 for pc in position_comparisons if pc.get("advantage") == "theirs"
        )
        if my_adv > their_adv:
            insights.append(
                f"You have the stronger squad in {my_adv} out of {len(position_comparisons)} positions"
            )
        elif their_adv > my_adv:
            insights.append(
                f"Your rival has the stronger squad in {their_adv} out of {len(position_comparisons)} positions"
            )

        if my_summary.get("total_transfers", 0) < rival_summary.get(
            "total_transfers", 0
        ):
            insights.append("You've been more transfer-efficient this season")
        elif rival_summary.get("total_transfers", 0) < my_summary.get(
            "total_transfers", 0
        ):
            insights.append("Your rival has been more transfer-efficient this season")

        return advantages, insights

    # ---------- Public endpoints ----------
    def analyze_team(
        self, fpl_team_id: int, include_team_obj: Optional[Team] = None
    ) -> Dict[str, Any]:
        """
        Main entry to analyze a team. Returns a serializable dict with:
          - team_summary, transfer_recommendations, captain_suggestions, budget_insights, analysis_timestamp
        """
        self.ensure_data()
        try:
            gw = self.current_gw or self.api.fetch_current_gameweek()
            team_picks = self.api.fetch_team_picks(fpl_team_id, gw)
            if not team_picks:
                return {"error": "Team picks not found"}

            current_squad = team_picks.get("picks", [])
            transfer_recs = self.analyze_transfer_priorities(current_squad)
            captain_suggestions = self.get_captain_suggestions(current_squad)
            squad_analysis = self.analyze_squad_metrics(current_squad)
            team_info = self.api.fetch_team_basic_info(int(fpl_team_id)) or {}

            team_summary = self.create_team_summary(
                team_info, team_picks, squad_analysis
            )

            if include_team_obj:
                team_summary["django_team"] = {
                    "id": include_team_obj.id,
                    "manager_name": include_team_obj.manager_name,
                    "last_synced": (
                        include_team_obj.last_synced_at.isoformat()
                        if include_team_obj.last_synced_at
                        else None
                    ),
                    "bank": include_team_obj.bank_in_millions,
                }

            budget = float(team_summary.get("bank", 0))
            free_transfers = team_info.get("entry_history", {}).get(
                "event_transfers_cost", 1
            )
            budget_insights = []
            if budget >= 5:
                budget_insights.append(
                    f"Strong budget position (£{budget:.1f}M) allows premium upgrades"
                )
            elif budget >= 2:
                budget_insights.append(
                    f"Decent budget (£{budget:.1f}M) for mid-range improvements"
                )
            else:
                budget_insights.append(
                    f"Tight budget (£{budget:.1f}M) - consider generating funds first"
                )

            if free_transfers >= 2:
                budget_insights.append(
                    f"Multiple free transfers ({free_transfers}) available"
                )
            elif free_transfers == 1:
                budget_insights.append("One free transfer - choose wisely")
            else:
                budget_insights.append("No free transfers - changes will cost points")

            return {
                "team_id": fpl_team_id,
                "team_summary": team_summary,
                "current_gameweek": gw,
                "transfer_recommendations": transfer_recs,
                "captain_suggestions": captain_suggestions,
                "budget_insights": budget_insights,
                "analysis_timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        except Exception as e:
            logger.exception("Analyze team failed: %s", e)
            return {"error": str(e)}

    # def compare_teams(self, my_fpl_team_id: int, rival_fpl_team_id: int, include_models: Optional[Tuple[Team, Team]] = None) -> Dict[str, Any]:
    #     """
    #     Compare two teams and produce TeamComparison-like dict.
    #     """
    #     self.ensure_data()
    #     try:
    #         gw = self.current_gw or self.api.fetch_current_gameweek()
    #         my_picks = self.api.fetch_team_picks(my_fpl_team_id, gw)
    #         rival_picks = self.api.fetch_team_picks(rival_fpl_team_id, gw)
    #         my_info = self.api.fetch_team_basic_info(my_fpl_team_id) or {}
    #         rival_info = self.api.fetch_team_basic_info(rival_fpl_team_id) or {}

    #         my_analysis = self.analyze_squad_metrics(my_picks.get("picks", []))
    #         rival_analysis = self.analyze_squad_metrics(rival_picks.get("picks", []))

    #         my_summary = self.create_team_summary(my_info, my_picks, my_analysis)
    #         rival_summary = self.create_team_summary(rival_info, rival_picks, rival_analysis)

    #         if include_models:
    #             my_model, rival_model = include_models
    #             my_summary["django_team"] = {
    #                 "id": my_model.id,
    #                 "last_synced": my_model.last_synced_at.isoformat() if my_model.last_synced_at else None,
    #                 "active": my_model.active
    #             }
    #             rival_summary["django_team"] = {
    #                 "id": rival_model.id,
    #                 "last_synced": rival_model.last_synced_at.isoformat() if rival_model.last_synced_at else None,
    #                 "active": rival_model.active
    #             }

    #         pos_comparisons = self.compare_positions(my_picks.get("picks", []), rival_picks.get("picks", []))
    #         advantages, insights = self.generate_head_to_head_insights(my_summary, rival_summary, pos_comparisons)

    #         return {
    #             "my_team": my_summary,
    #             "rival_team": rival_summary,
    #             "position_comparisons": pos_comparisons,
    #             "head_to_head_advantages": advantages,
    #             "key_insights": insights,
    #             "analysis_timestamp": datetime.utcnow().isoformat()
    #         }
    #     except Exception as e:
    #         logger.exception("Compare teams failed: %s", e)
    #         return {"error": str(e)}
