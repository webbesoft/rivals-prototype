# rivals/services/fpl_analysis.py
import hashlib
import logging
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from django.core.cache import cache
from django.db.models import Max
from django.utils import timezone

from rivals.models import SyncState

from ..models import Team
from .fpl_api_service import FplApiService
from .gameweek_context_service import GameweekContextService

logger = logging.getLogger(__name__)


class TeamAnalysisService:
    """
    Analysis of published FPL snapshots; explicit API injection supports worker use.
    Methods:
      - refresh_data()  -> preload bootstrap & fixtures
      - analyze_team(fpl_team_id, include_team_obj=None) -> Dict analysis result
      - compare_teams(my_fpl_team_id, rival_fpl_team_id, include_models=None) -> Dict comparison
    """

    def __init__(self, api: Optional[FplApiService] = None):
        self.api = api or FplApiService(snapshot_only=True)
        self.bootstrap: Optional[Dict[str, Any]] = None
        self.elements: List[Dict[str, Any]] = []
        self.teams: Dict[int, Dict[str, Any]] = {}
        self.events: List[Dict[str, Any]] = []
        self.fixtures: List[Dict[str, Any]] = []
        self.current_gw = None
        self.decision_gw = None
        self._fixture_difficulty_cache = {}

    # ---------- Data loading / caching ----------
    def refresh_data(self, force_refresh: bool = False) -> bool:
        """
        Fetch bootstrap-static and fixtures. Returns True if success.
        Uses cached data via FplApiService by default unless force_refresh=True.
        """
        try:
            bootstrap = self.api.fetch_bootstrap_static(force_refresh=force_refresh)
            if not bootstrap:
                logger.error("FPL bootstrap-static fetch returned None")
                return False

            self.bootstrap = bootstrap
            self.elements = bootstrap.get("elements", [])
            self.teams = {t["id"]: t for t in bootstrap.get("teams", [])}
            self.events = bootstrap.get("events", [])
            event_context = GameweekContextService.derive(self.events)
            self.current_gw = event_context["scoring_gameweek"]
            self.decision_gw = event_context["decision_gameweek"]
            self._fixture_difficulty_cache.clear()

            fixtures = self.api.fetch_fixtures_data(force_refresh=force_refresh)
            if fixtures is None:
                return False
            self.fixtures = fixtures
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
        if self.bootstrap is None:
            ok = self.refresh_data()
            if not ok:
                raise RuntimeError("Unable to load FPL bootstrap/fixtures data")

    # ---------- Utility helpers ----------
    def _comparison_picks(self, fpl_team_id, gameweek):
        picks = self.api.fetch_team_picks(fpl_team_id, gameweek)
        if picks:
            return picks

        # Transport snapshots expire independently of the persisted lineup.
        # Never substitute a previous event or season's squad for this event.
        season = GameweekContextService.derive(self.events)["season"]
        if not season:
            return None
        team = Team.objects.filter(fpl_team_id=str(fpl_team_id)).first()
        if not team:
            return None
        rows = list(
            team.squad_histories.filter(season=season, gameweek=gameweek).order_by(
                "position"
            )
        )
        if len(rows) != 15:
            return None
        history = team.gameweek_data.filter(season=season, gameweek=gameweek).first()
        return {
            "picks": [
                {
                    "element": row.player_fpl_id,
                    "position": row.position,
                    "multiplier": row.multiplier,
                    "is_captain": row.is_captain,
                    "is_vice_captain": row.is_vice_captain,
                }
                for row in rows
            ],
            "entry_history": {
                "bank": history.bank if history else None,
                "value": sum(row.player_cost for row in rows),
            },
        }

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
        memo_key = (team_id, next_fixtures)
        if memo_key in self._fixture_difficulty_cache:
            return self._fixture_difficulty_cache[memo_key]
        fixtures = [
            f
            for f in self.fixtures
            if not f.get("finished")
            and f.get("event") is not None
            and (self.decision_gw is None or f["event"] >= self.decision_gw)
        ]
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
        result = sum(weighted) / len(weighted)
        self._fixture_difficulty_cache[memo_key] = result
        return result

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
        excluded = set(exclude_ids)
        players = [
            p
            for p in self.elements
            if p.get("element_type") == position_id
            and p.get("id") not in excluded
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
            alternatives = self.get_top_alternatives_by_position(
                position_id, current_player_ids, limit=10
            )
            for pinfo in players:
                player = pinfo["player"]
                fixture_difficulty = self.calculate_fixture_difficulty(
                    player["team"], 5
                )
                expected_current = self.calculate_expected_points(
                    player, fixture_difficulty
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
            upcoming = [
                fixture
                for fixture in self.fixtures
                if fixture.get("event") == self.decision_gw
                and player["team"] in (fixture.get("team_h"), fixture.get("team_a"))
                and not fixture.get("finished")
            ]
            if not self.decision_gw or not upcoming:
                continue
            fd = self.calculate_fixture_difficulty(player["team"], next_fixtures=1)
            expected = (self.calculate_expected_points(player, fd) / 5.0) * len(
                upcoming
            )
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
            "bank": (
                float(team_picks["entry_history"]["bank"]) / 10.0
                if team_picks.get("entry_history", {}).get("bank") is not None
                else None
            ),
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
                ("mine" if my_summary["bank"] > rival_summary["bank"] else "theirs")
                if my_summary.get("bank") is not None
                and rival_summary.get("bank") is not None
                else None
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

    def analysis_cache_key(self, team, context, free_transfers=None):
        keys = ["context", "players"] + [
            f"{kind}:{team.id}" for kind in ("profile", "history", "transfers")
        ]
        versions = list(
            SyncState.objects.filter(key__in=keys)
            .order_by("key")
            .values_list("key", "last_success_at")
        )
        squad_version = team.squad_histories.filter(
            season=context["season"], gameweek=context["scoring_gameweek"]
        ).aggregate(latest=Max("updated_at"))["latest"]
        stamp = (
            context["season"],
            context["scoring_gameweek"],
            context["decision_gameweek"],
            versions,
            squad_version,
            team.bank,
            team.current_total_points,
            free_transfers,
        )
        digest = hashlib.sha256(repr(stamp).encode()).hexdigest()[:24]
        return f"tactical_v3_{team.fpl_team_id}_{digest}"

    def get_cached_analysis(
        self, fpl_team_id, include_team_obj=None, free_transfers=None
    ):
        context = GameweekContextService.cached()
        team = (
            include_team_obj
            or Team.objects.filter(fpl_team_id=str(fpl_team_id)).first()
        )
        if (
            not team
            or context["stale"]
            or not context["scoring_gameweek"]
            or not context["decision_gameweek"]
        ):
            return None
        return cache.get(self.analysis_cache_key(team, context, free_transfers))

    def analyze_team(
        self,
        fpl_team_id,
        include_team_obj=None,
        force_refresh=False,
        free_transfers=None,
    ):
        context = GameweekContextService.cached()
        team = (
            include_team_obj
            or Team.objects.filter(fpl_team_id=str(fpl_team_id)).first()
        )
        if context["stale"] or not team or team.current_season != context["season"]:
            return {
                "error": "Your briefing is temporarily unavailable. Please try again shortly."
            }
        if not context["scoring_gameweek"]:
            return {
                "error": "Your briefing will be available after your first gameweek deadline."
            }
        if not context["decision_gameweek"]:
            return {"error": "There is no upcoming gameweek to prepare for yet."}
        key = self.analysis_cache_key(team, context, free_transfers)
        if not force_refresh:
            cached = cache.get(key)
            if cached:
                return cached
        try:
            self.api.TIMEOUT = 4
            self.ensure_data()
            actual = GameweekContextService.derive(self.events)
            if (
                actual["season"],
                actual["scoring_gameweek"],
                actual["decision_gameweek"],
            ) != (
                context["season"],
                context["scoring_gameweek"],
                context["decision_gameweek"],
            ):
                return {
                    "error": "The next gameweek details are updating. Please try again shortly."
                }
            gw = context["scoring_gameweek"]
            squad_rows = list(
                team.squad_histories.filter(
                    season=context["season"], gameweek=gw
                ).order_by("position")
            )
            if len(squad_rows) == 15:
                team_picks = {
                    "picks": [
                        {
                            "element": row.player_fpl_id,
                            "position": row.position,
                            "multiplier": row.multiplier,
                            "is_captain": row.is_captain,
                            "is_vice_captain": row.is_vice_captain,
                        }
                        for row in squad_rows
                    ],
                    "entry_history": {
                        "bank": (
                            team.bank
                            if team.gameweek_data.filter(
                                season=context["season"], gameweek=gw
                            ).exists()
                            else None
                        ),
                        "value": team.value,
                    },
                }
            else:
                team_picks = self.api.fetch_team_picks(fpl_team_id, gw)
            if not team_picks or len(team_picks.get("picks", [])) != 15:
                return {
                    "error": "Your full squad is currently unavailable. Please try again."
                }
            current_squad = team_picks["picks"]
            if not {pick["element"] for pick in current_squad}.issubset(
                {p["id"] for p in self.elements}
            ):
                return {
                    "error": "Some player details are updating. Please try again shortly."
                }
            team_info = {
                "id": fpl_team_id,
                "name": team.team_name,
                "summary_overall_points": team.current_total_points,
                "summary_event_points": team.current_event_points,
                "summary_overall_rank": team.current_overall_rank,
                "summary_total_transfers": team.total_transfers,
            }
            summary = self.create_team_summary(
                team_info, team_picks, self.analyze_squad_metrics(current_squad)
            )
            budget_insights = [
                (
                    f"£{summary['bank']:.1f}m last reported bank. Check your current balance before making a move."
                    if summary["bank"] is not None
                    else "Check your current FPL balance before making a move."
                )
            ]
            if free_transfers is None:
                budget_insights.append(
                    "Enter your available free transfers to help review your next move."
                )
            else:
                budget_insights.append(
                    f"You entered {free_transfers} free transfers for GW{context['decision_gameweek']}."
                )
            result = {
                "team_id": fpl_team_id,
                "team_summary": summary,
                "season": context["season"],
                "current_gameweek": gw,
                "decision_gameweek": context["decision_gameweek"],
                "free_transfers": free_transfers,
                "transfer_recommendations": self.analyze_transfer_priorities(
                    current_squad
                ),
                "captain_suggestions": self.get_captain_suggestions(current_squad),
                "budget_insights": budget_insights,
                "analysis_timestamp": timezone.now().strftime("%Y-%m-%d %H:%M %Z"),
            }
            cache.set(key, result, 120)
            return result
        except Exception:
            logger.exception("Analyze team failed for %s", fpl_team_id)
            return {"error": "We couldn't prepare your briefing. Please try again."}

    def get_captain_projection(
        self, squad_picks: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """Get projected captain points for next GW."""
        captain_suggestions = self.get_captain_suggestions(
            [pick for pick in squad_picks if pick.get("is_captain")], limit=1
        )
        if captain_suggestions:
            captain = captain_suggestions[0]
            return {
                "name": captain["name"],
                "team": captain["team"],
                "expected_points": captain["expected_points_next_gw"]
                * 2,  # Captain gets 2x
                "form": captain["form"],
            }
        return None

    def calculate_momentum(self, team_model: Team, last_n: int = 5) -> Dict[str, Any]:
        """Calculate momentum from last N gameweeks."""
        recent_gws = (
            team_model.gameweek_data.filter(season=team_model.current_season)
            .order_by("-gameweek")[:last_n]
            .values_list("points", flat=True)
        )
        points_list = list(recent_gws)
        if not points_list:
            return {"average": None, "trend": None, "points": []}

        avg = sum(points_list) / len(points_list)
        if len(points_list) >= 2:
            trend = "up" if points_list[0] > points_list[-1] else "down"
        else:
            trend = "neutral"

        return {
            "average": round(avg, 1),
            "trend": trend,
            "points": points_list,
        }

    def calculate_squad_difficulty_schedule(
        self, squad_picks: List[Dict[str, Any]], next_gw: int, duration: int = 5
    ) -> List[Dict[str, Any]]:
        """
        Calculate the average fixture difficulty for the squad for upcoming gameweeks.
        Returns a list of dicts with gameweek and average difficulty score.
        """
        schedule = []
        # optimization: pre-fetch all player definitions
        squad_players = []
        for pick in squad_picks:
            p = next((x for x in self.elements if x["id"] == pick["element"]), None)
            if p:
                squad_players.append(p)

        if not squad_players:
            return []

        # We assume self.fixtures is already loaded and sorted/filterable
        # But efficiently mapping team/GW to difficulty requires a lookup
        # Let's build a quick lookup: (team_id, event) -> difficulty
        fixture_lookup = defaultdict(list)
        upcoming_fixtures = [
            f
            for f in self.fixtures
            if f.get("event") and f.get("event") >= next_gw and not f.get("finished")
        ]

        for f in upcoming_fixtures:
            evt = f["event"]
            if evt >= next_gw + duration:
                continue

            # For double gameweeks, a team might have multiple fixtures
            # We will average them for that GW if multiple exist
            fixture_lookup[(f["team_h"], evt)].append(f.get("team_h_difficulty", 3))
            fixture_lookup[(f["team_a"], evt)].append(f.get("team_a_difficulty", 3))

        for gw in range(next_gw, next_gw + duration):
            total_difficulty = 0
            count = 0

            for player in squad_players:
                team_id = player["team"]
                difficulties = fixture_lookup.get((team_id, gw), [])

                if not difficulties:
                    # Blank gameweek for this player? Or just no data.
                    # Assign neutral or penalty? Let's use 3 (neutral) but it might be misleading for blanks.
                    # For now, treat as average difficulty.
                    # Ideally we should flag blanks, but for a difficulty score, 0 would drag it down misleadingly (easier?)
                    # or 5 (hard). Let's stick to 4 to penalize blanks slightly in difficulty view,
                    # or keep it simple with 3.
                    pass
                else:
                    # Average difficulty for this player this GW (e.g. DGW)
                    avg_diff = sum(difficulties) / len(difficulties)
                    total_difficulty += avg_diff
                    count += 1

            if count > 0:
                squad_avg = total_difficulty / count
            else:
                squad_avg = 0.0  # No players have fixtures?

            schedule.append(
                {
                    "gameweek": gw,
                    "difficulty_score": round(squad_avg, 2),
                    "rating": (
                        "easy"
                        if squad_avg <= 2.5
                        else "hard" if squad_avg >= 3.5 else "medium"
                    ),
                }
            )

        return schedule

    def get_fixture_schedule(
        self, team_id: int, next_fixtures: int = 5
    ) -> List[Dict[str, Any]]:
        """Get upcoming fixture schedule with difficulty ratings."""
        fixtures = [
            f
            for f in self.fixtures
            if not f.get("finished") and f.get("event") is not None
        ]
        team_fixtures = []
        for fx in fixtures:
            event = fx.get("event")
            if fx.get("team_h") == team_id:
                team_fixtures.append(
                    {
                        "gameweek": event,
                        "difficulty": fx.get("team_h_difficulty", 3),
                        "opponent": self.teams.get(fx.get("team_a"), {}).get(
                            "short_name", "?"
                        ),
                        "is_home": True,
                    }
                )
            elif fx.get("team_a") == team_id:
                team_fixtures.append(
                    {
                        "gameweek": event,
                        "difficulty": fx.get("team_a_difficulty", 3),
                        "opponent": self.teams.get(fx.get("team_h"), {}).get(
                            "short_name", "?"
                        ),
                        "is_home": False,
                    }
                )

        return sorted(team_fixtures, key=lambda x: x["gameweek"])[:next_fixtures]

    def quick_compare(
        self,
        my_fpl_team_id: int,
        rival_fpl_team_id: int,
        my_team_model: Optional[Team] = None,
        rival_team_model: Optional[Team] = None,
    ) -> Dict[str, Any]:
        """Lightweight comparison for quick panel display."""
        try:
            self.api.TIMEOUT = 4
            self.ensure_data()
            gw = self.current_gw
            if not gw:
                return {"error": "Lineups are not available for comparison yet."}
            my_picks = self._comparison_picks(my_fpl_team_id, gw)
            rival_picks = self._comparison_picks(rival_fpl_team_id, gw)

            if not my_picks or not rival_picks:
                return {
                    "error": "Team lineups are being refreshed. Please try again shortly."
                }

            my_squad = my_picks.get("picks", [])
            rival_squad = rival_picks.get("picks", [])

            # Expected points next GW
            my_metrics = self.analyze_squad_metrics(my_squad)
            rival_metrics = self.analyze_squad_metrics(rival_squad)
            my_expected_next = (
                (my_metrics["total_expected_points"] / 5.0)
                if my_metrics.get("total_expected_points")
                else 0
            )
            rival_expected_next = (
                (rival_metrics["total_expected_points"] / 5.0)
                if rival_metrics.get("total_expected_points")
                else 0
            )
            expected_diff = my_expected_next - rival_expected_next

            # Captaincy
            my_captain = self.get_captain_projection(my_squad)
            rival_captain = self.get_captain_projection(rival_squad)
            captain_diff = 0.0
            if my_captain and rival_captain:
                captain_diff = (
                    my_captain["expected_points"] - rival_captain["expected_points"]
                )

            # FDR
            my_fdr = my_metrics.get("avg_fixture_difficulty", 3.0)
            rival_fdr = rival_metrics.get("avg_fixture_difficulty", 3.0)

            # FD Schedule
            my_schedule = self.calculate_squad_difficulty_schedule(my_squad, gw, 5)
            rival_schedule = self.calculate_squad_difficulty_schedule(
                rival_squad, gw, 5
            )

            # Momentum
            my_momentum = (
                self.calculate_momentum(my_team_model)
                if my_team_model
                else {"average": None, "trend": None}
            )
            rival_momentum = (
                self.calculate_momentum(rival_team_model)
                if rival_team_model
                else {"average": None, "trend": None}
            )
            momentum_diff = (
                my_momentum["average"] - rival_momentum["average"]
                if my_momentum["average"] is not None
                and rival_momentum["average"] is not None
                else None
            )

            # Available free transfers are private team information.
            my_free_transfers = None
            rival_free_transfers = None

            return {
                "expected_points": {
                    "mine": round(my_expected_next, 1),
                    "rival": round(rival_expected_next, 1),
                    "difference": round(expected_diff, 1),
                },
                "captaincy": {
                    "mine": my_captain,
                    "rival": rival_captain,
                    "difference": round(captain_diff, 1),
                },
                "fixture_difficulty": {
                    "mine": round(my_fdr, 2),
                    "rival": round(rival_fdr, 2),
                    "mine_schedule": my_schedule,
                    "rival_schedule": rival_schedule,
                },
                "momentum": {
                    "mine": my_momentum,
                    "rival": rival_momentum,
                    "difference": (
                        round(momentum_diff, 1) if momentum_diff is not None else None
                    ),
                },
                "transfers": {
                    "mine_free": my_free_transfers,
                    "rival_free": rival_free_transfers,
                },
                "current_gameweek": gw,
            }
        except Exception as e:
            logger.exception("Quick compare failed: %s", e)
            return {
                "error": "This comparison is temporarily unavailable. Please try again."
            }

    def compare_teams(
        self,
        my_fpl_team_id: int,
        rival_fpl_team_id: int,
        include_models: Optional[Tuple[Team, Team]] = None,
    ) -> Dict[str, Any]:
        """
        Compare two teams and produce comprehensive comparison dict.
        """
        try:
            self.api.TIMEOUT = 4
            self.ensure_data()
            gw = self.current_gw
            if not gw:
                return {"error": "Lineups are not available for comparison yet."}
            my_picks = self._comparison_picks(my_fpl_team_id, gw)
            rival_picks = self._comparison_picks(rival_fpl_team_id, gw)

            if not my_picks or not rival_picks:
                return {
                    "error": "Team lineups are being refreshed. Please try again shortly."
                }

            my_info = self.api.fetch_team_basic_info(my_fpl_team_id) or {}
            rival_info = self.api.fetch_team_basic_info(rival_fpl_team_id) or {}

            my_analysis = self.analyze_squad_metrics(my_picks.get("picks", []))
            rival_analysis = self.analyze_squad_metrics(rival_picks.get("picks", []))

            my_summary = self.create_team_summary(my_info, my_picks, my_analysis)
            rival_summary = self.create_team_summary(
                rival_info, rival_picks, rival_analysis
            )

            if include_models:
                my_model, rival_model = include_models
                my_summary["django_team"] = {
                    "id": my_model.id,
                    "last_synced": (
                        my_model.last_synced_at.isoformat()
                        if my_model.last_synced_at
                        else None
                    ),
                    "active": my_model.active,
                }
                rival_summary["django_team"] = {
                    "id": rival_model.id,
                    "last_synced": (
                        rival_model.last_synced_at.isoformat()
                        if rival_model.last_synced_at
                        else None
                    ),
                    "active": rival_model.active,
                }

            pos_comparisons = self.compare_positions(
                my_picks.get("picks", []), rival_picks.get("picks", [])
            )
            advantages, insights = self.generate_head_to_head_insights(
                my_summary, rival_summary, pos_comparisons
            )

            # Get captain projections
            my_captain = self.get_captain_projection(my_picks.get("picks", []))
            rival_captain = self.get_captain_projection(rival_picks.get("picks", []))

            # Get momentum
            my_model = include_models[0] if include_models else None
            rival_model = include_models[1] if include_models else None
            my_momentum = (
                self.calculate_momentum(my_model)
                if my_model
                else {"average": None, "trend": None}
            )
            rival_momentum = (
                self.calculate_momentum(rival_model)
                if rival_model
                else {"average": None, "trend": None}
            )

            # Get fixture schedules (squad aggregate)
            my_fixtures = self.calculate_squad_difficulty_schedule(
                my_picks.get("picks", []), gw, 6
            )
            rival_fixtures = self.calculate_squad_difficulty_schedule(
                rival_picks.get("picks", []), gw, 6
            )

            # Transfer recommendations
            my_transfer_recs = self.analyze_transfer_priorities(
                my_picks.get("picks", []), top_k=3
            )
            rival_transfer_recs = self.analyze_transfer_priorities(
                rival_picks.get("picks", []), top_k=3
            )

            # Projected GW outcome
            my_expected_next = (
                (my_analysis["total_expected_points"] / 5.0)
                if my_analysis.get("total_expected_points")
                else 0
            )
            rival_expected_next = (
                (rival_analysis["total_expected_points"] / 5.0)
                if rival_analysis.get("total_expected_points")
                else 0
            )
            projected_diff = my_expected_next - rival_expected_next

            return {
                "my_team": my_summary,
                "rival_team": rival_summary,
                # Keep missing entry totals distinct from a genuine zero. The
                # database profile can lag behind this comparison's FPL fetches.
                "season_totals": {
                    "mine": my_info.get("summary_overall_points"),
                    "rival": rival_info.get("summary_overall_points"),
                },
                "position_comparisons": pos_comparisons,
                "head_to_head_advantages": advantages,
                "key_insights": insights,
                "captaincy": {
                    "mine": my_captain,
                    "rival": rival_captain,
                },
                "momentum": {
                    "mine": my_momentum,
                    "rival": rival_momentum,
                },
                "fixture_schedules": {
                    "mine": my_fixtures,
                    "rival": rival_fixtures,
                },
                "transfer_recommendations": {
                    "mine": my_transfer_recs,
                    "rival": rival_transfer_recs,
                },
                "projected_gw_outcome": {
                    "my_expected": round(my_expected_next, 1),
                    "rival_expected": round(rival_expected_next, 1),
                    "difference": round(projected_diff, 1),
                },
                "current_gameweek": gw,
                "analysis_timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        except Exception as e:
            logger.exception("Compare teams failed: %s", e)
            return {
                "error": "This comparison is temporarily unavailable. Please try again."
            }
