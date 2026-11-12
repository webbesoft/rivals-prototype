import logging
import math
from typing import Any, Dict, List, Optional

from django.core.cache import cache
from django.db.models import Case, Count, F, IntegerField, Q, Value, When

from rivals.models import GameweekData, MiniLeague, MiniLeagueTeam, Team
from rivals.services.fpl_api_service import FplApiService
from rivals.services.gameweek_context_service import GameweekContextService

logger = logging.getLogger(__name__)

TOTAL_GAMEWEEKS = 38

# Historical FPL calibration curve: (Final Points, Overall Rank)
# Derived from end-of-season historical distributions across 10M+ managers
RANK_BENCHMARKS = [
    (2750, 250),
    (2650, 1000),
    (2550, 5000),
    (2500, 10000),
    (2430, 25000),
    (2380, 50000),
    (2330, 100000),
    (2260, 250000),
    (2180, 500000),
    (2080, 1000000),
    (1950, 2000000),
    (1800, 4000000),
    (1650, 6500000),
    (1450, 9000000),
    (1200, 10500000),
]


class ProjectionService:
    """
    Service to calculate projected finishing points totals and overall/mini-league
    ranks for FPL teams.
    """

    def __init__(self, api: Optional[FplApiService] = None, *, context=None):
        self.api = api or FplApiService()
        self.context = context

    def _gameweek_context(self):
        if self.context is None:
            self.context = GameweekContextService.cached()
        return self.context

    # ------------------------------------------------------------------
    # Core mathematical conversion
    # ------------------------------------------------------------------

    @staticmethod
    def points_to_overall_rank(points: int) -> int:
        """
        Interpolates expected final overall rank based on final points total
        using log-linear interpolation across calibrated FPL benchmark percentiles.
        """
        if points >= RANK_BENCHMARKS[0][0]:
            # Above highest benchmark
            excess = points - RANK_BENCHMARKS[0][0]
            estimated = RANK_BENCHMARKS[0][1] - (excess * 2)
            return max(1, int(round(estimated)))

        if points <= RANK_BENCHMARKS[-1][0]:
            # Below lowest benchmark
            deficit = RANK_BENCHMARKS[-1][0] - points
            estimated = RANK_BENCHMARKS[-1][1] + (deficit * 1500)
            return min(11_500_000, int(round(estimated)))

        # Find bracket
        for i in range(len(RANK_BENCHMARKS) - 1):
            p_high, r_low = RANK_BENCHMARKS[i]
            p_low, r_high = RANK_BENCHMARKS[i + 1]

            if p_low <= points <= p_high:
                # Log-linear interpolation
                ratio = (p_high - points) / max(1, (p_high - p_low))
                log_r_low = math.log(r_low)
                log_r_high = math.log(r_high)
                interpolated_log = log_r_low + ratio * (log_r_high - log_r_low)
                return int(round(math.exp(interpolated_log)))

        return 1_000_000

    @staticmethod
    def get_rank_tier_label(rank: int) -> str:
        """Returns human-friendly tier text e.g. 'Top 10k', 'Top 50k', 'Top 1M'."""
        if rank <= 1_000:
            return "Top 1k"
        elif rank <= 10_000:
            return "Top 10k"
        elif rank <= 50_000:
            return "Top 50k"
        elif rank <= 100_000:
            return "Top 100k"
        elif rank <= 250_000:
            return "Top 250k"
        elif rank <= 50_0000:
            return "Top 500k"
        elif rank <= 1_000_000:
            return "Top 1M"
        elif rank <= 2_000_000:
            return "Top 2M"
        elif rank <= 5_000_000:
            return "Top 5M"
        return "Top 10M"

    # ------------------------------------------------------------------
    # Team Projection
    # ------------------------------------------------------------------

    def project_team_finish(
        self, team: Team, current_gw: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Projects a team's finishing points total and final overall rank.
        Takes into account current points, season PPG, and recent 5-GW form.
        """
        # Determine completed gameweeks
        gw_query = team.gameweek_data
        if team.current_season:
            gw_query = gw_query.filter(season=team.current_season)

        completed_gws_qs = gw_query.filter(points__isnull=False).order_by("-gameweek")
        completed_count = completed_gws_qs.count()

        if completed_count == 0:
            # Use stored event context when history has not yet been imported.
            live_gw = current_gw or self._gameweek_context()["scoring_gameweek"]
            if live_gw is None:
                return {}
            completed_count = max(0, live_gw - 1)

        completed_count = min(TOTAL_GAMEWEEKS, max(0, completed_count))
        remaining_gws = TOTAL_GAMEWEEKS - completed_count

        current_points = team.current_total_points or 0
        current_rank = team.current_overall_rank

        # Handle edge cases
        if completed_count >= TOTAL_GAMEWEEKS:
            return {
                "current_points": current_points,
                "current_overall_rank": current_rank,
                "completed_gameweeks": TOTAL_GAMEWEEKS,
                "remaining_gameweeks": 0,
                "season_ppg": round(current_points / TOTAL_GAMEWEEKS, 1),
                "recent_ppg": round(current_points / TOTAL_GAMEWEEKS, 1),
                "projected_ppg": round(current_points / TOTAL_GAMEWEEKS, 1),
                "projected_points": current_points,
                "projected_points_min": current_points,
                "projected_points_max": current_points,
                "projected_overall_rank": current_rank,
                "projected_rank_min": current_rank,
                "projected_rank_max": current_rank,
                "projected_rank_tier": self.get_rank_tier_label(
                    current_rank or 1000000
                ),
                "rank_trajectory": "steady",
                "rank_change": 0,
                "season_completed": True,
            }

        if completed_count == 0:
            # Pre-season baseline
            default_pace = 52.0
            projected_pts = int(round(default_pace * TOTAL_GAMEWEEKS))
            proj_rank = self.points_to_overall_rank(projected_pts)
            return {
                "current_points": 0,
                "current_overall_rank": None,
                "completed_gameweeks": 0,
                "remaining_gameweeks": TOTAL_GAMEWEEKS,
                "season_ppg": default_pace,
                "recent_ppg": default_pace,
                "projected_ppg": default_pace,
                "projected_points": projected_pts,
                "projected_points_min": int(round(projected_pts * 0.9)),
                "projected_points_max": int(round(projected_pts * 1.1)),
                "projected_overall_rank": proj_rank,
                "projected_rank_min": self.points_to_overall_rank(
                    int(round(projected_pts * 1.1))
                ),
                "projected_rank_max": self.points_to_overall_rank(
                    int(round(projected_pts * 0.9))
                ),
                "projected_rank_tier": self.get_rank_tier_label(proj_rank),
                "rank_trajectory": "steady",
                "rank_change": 0,
                "season_completed": False,
            }

        # Calculate Season PPG
        season_ppg = current_points / completed_count

        # Calculate Recent Form PPG (last 5 completed gameweeks)
        recent_gws = list(completed_gws_qs[:5])
        if recent_gws:
            recent_ppg = sum(gw.points for gw in recent_gws) / len(recent_gws)
        else:
            recent_ppg = season_ppg

        # Blend Season PPG and Form PPG for expected pace
        if completed_count < 5:
            projected_ppg = season_ppg
        else:
            # 60% season average, 40% recent form momentum
            projected_ppg = (0.6 * season_ppg) + (0.4 * recent_ppg)

        # Conservative and optimistic scenarios
        conservative_ppg = max(25.0, min(season_ppg, recent_ppg) * 0.92)
        optimistic_ppg = max(season_ppg, recent_ppg) * 1.08

        # Projected Points
        expected_final_points = int(
            round(current_points + (remaining_gws * projected_ppg))
        )
        min_final_points = int(
            round(current_points + (remaining_gws * conservative_ppg))
        )
        max_final_points = int(round(current_points + (remaining_gws * optimistic_ppg)))

        # Projected Ranks
        projected_rank = self.points_to_overall_rank(expected_final_points)
        optimistic_rank = self.points_to_overall_rank(max_final_points)
        conservative_rank = self.points_to_overall_rank(min_final_points)

        # Rank Trajectory
        rank_change = 0
        rank_trajectory = "steady"
        if current_rank:
            # Higher rank number means worse position (e.g. 50,000 -> 30,000 is climbing +20,000 spots)
            rank_diff = current_rank - projected_rank
            rank_change = rank_diff
            if rank_diff > 2000:
                rank_trajectory = "rising"
            elif rank_diff < -2000:
                rank_trajectory = "falling"

        return {
            "current_points": current_points,
            "current_overall_rank": current_rank,
            "completed_gameweeks": completed_count,
            "remaining_gameweeks": remaining_gws,
            "season_ppg": round(season_ppg, 1),
            "recent_ppg": round(recent_ppg, 1),
            "projected_ppg": round(projected_ppg, 1),
            "projected_points": expected_final_points,
            "projected_points_min": min_final_points,
            "projected_points_max": max_final_points,
            "projected_overall_rank": projected_rank,
            "projected_rank_min": optimistic_rank,
            "projected_rank_max": conservative_rank,
            "projected_rank_tier": self.get_rank_tier_label(projected_rank),
            "rank_trajectory": rank_trajectory,
            "rank_change": rank_change,
            "season_completed": False,
        }

    # ------------------------------------------------------------------
    # Mini-League Projection
    # ------------------------------------------------------------------

    def project_mini_league(
        self,
        mini_league: MiniLeague,
        current_gw: Optional[int] = None,
        *,
        standings=None,
    ) -> Dict[str, Any]:
        """
        Projects final standings and finishing positions for all teams in a mini-league.
        """
        context = self._gameweek_context()
        if mini_league.uses_summary:
            return {
                "league_id": mini_league.id,
                "standings": [],
                "total_teams": mini_league.rank_count,
                "partial": True,
            }
        live_gw = current_gw or context["scoring_gameweek"]
        synced_at = (
            mini_league.last_synced_at.isoformat()
            if mini_league.last_synced_at
            else "never"
        )
        cache_key = (
            f"ml_projection_{mini_league.id}_{context['season']}_{live_gw}_{synced_at}"
        )
        cached = cache.get(cache_key)
        if cached:
            return cached

        if live_gw is None:
            return {"league_id": mini_league.id, "standings": [], "total_teams": 0}
        completed_gw = max(0, min(TOTAL_GAMEWEEKS, live_gw - 1))
        remaining_gws = TOTAL_GAMEWEEKS - completed_gw

        mlts = (
            standings
            if standings is not None
            else list(mini_league.mini_league_teams.select_related("team").all())
        )
        if not mlts:
            return {"league_id": mini_league.id, "standings": [], "total_teams": 0}

        team_projections = []
        for mlt in mlts:
            team = mlt.team
            curr_pts = mlt.total_points or (team.current_total_points if team else 0)
            curr_rank = mlt.current_rank or 999

            # Calculate individual pace
            if completed_gw > 0:
                pace = curr_pts / completed_gw
            else:
                pace = 50.0

            proj_pts = int(round(curr_pts + (remaining_gws * pace)))
            proj_overall_rank = self.points_to_overall_rank(proj_pts)

            team_projections.append(
                {
                    "team_id": team.id if team else None,
                    "team_name": team.team_name if team else "Unknown",
                    "manager_name": team.manager_name if team else "Unknown",
                    "current_rank": curr_rank,
                    "current_points": curr_pts,
                    "pace_ppg": round(pace, 1),
                    "projected_points": proj_pts,
                    "projected_overall_rank": proj_overall_rank,
                }
            )

        # Sort by projected points descending
        team_projections.sort(key=lambda x: (-x["projected_points"], x["current_rank"]))

        # Assign projected ranks and calculate gap metrics
        leader_pts = team_projections[0]["projected_points"] if team_projections else 0
        third_place_pts = (
            team_projections[min(2, len(team_projections) - 1)]["projected_points"]
            if team_projections
            else 0
        )

        for idx, item in enumerate(team_projections):
            proj_rank = idx + 1
            item["projected_rank"] = proj_rank
            item["rank_change"] = item["current_rank"] - proj_rank  # + = gain, - = loss
            item["gap_to_leader"] = leader_pts - item["projected_points"]
            item["gap_to_podium"] = max(0, third_place_pts - item["projected_points"])

        result = {
            "league_id": mini_league.id,
            "league_name": mini_league.name,
            "completed_gameweeks": completed_gw,
            "remaining_gameweeks": remaining_gws,
            "leader": team_projections[0] if team_projections else None,
            "standings": team_projections,
            "total_teams": len(team_projections),
        }

        # Cache for 10 minutes
        cache.set(cache_key, result, 600)
        return result

    def get_team_mini_league_summary(
        self,
        team: Team,
        mini_league: MiniLeague,
        current_gw: Optional[int] = None,
        *,
        standings=None,
    ) -> Optional[Dict[str, Any]]:
        """
        Gets a single team's projected finish within a specific mini-league.
        """
        if standings is None:
            return self._team_mini_league_summary(team, mini_league, current_gw)
        league_proj = self.project_mini_league(
            mini_league, current_gw=current_gw, standings=standings
        )
        for s in league_proj.get("standings", []):
            if s["team_id"] == team.id:
                return {
                    "league_id": mini_league.id,
                    "league_name": mini_league.name,
                    "current_rank": s["current_rank"],
                    "projected_rank": s["projected_rank"],
                    "rank_change": s["rank_change"],
                    "projected_points": s["projected_points"],
                    "gap_to_leader": s["gap_to_leader"],
                    "gap_to_podium": s["gap_to_podium"],
                    "total_teams": league_proj["total_teams"],
                }
        return None

    def _team_mini_league_summary(self, team, mini_league, current_gw):
        """Compute a dashboard summary without materialising the league's teams.

        All entries use the same pace multiplier, so ordering by points gives
        the same projected order. Preserve the full projection's zero fallbacks
        and stable ordering for tied points/ranks.
        """
        if mini_league.uses_summary:
            return None
        live_gw = current_gw or self._gameweek_context()["scoring_gameweek"]
        if live_gw is None:
            return None
        rows = mini_league.mini_league_teams.annotate(
            summary_points=Case(
                When(total_points=0, then=F("team__current_total_points")),
                default=F("total_points"),
                output_field=IntegerField(),
            ),
            summary_rank=Case(
                When(current_rank=0, then=Value(999)),
                default=F("current_rank"),
                output_field=IntegerField(),
            ),
        )
        own = (
            rows.filter(team_id=team.pk)
            .values("pk", "summary_points", "summary_rank")
            .first()
        )
        if own is None:
            return None
        points, rank = own["summary_points"], own["summary_rank"]
        ahead = (
            Q(summary_points__gt=points)
            | Q(summary_points=points, summary_rank__lt=rank)
            | Q(summary_points=points, summary_rank=rank, pk__lt=own["pk"])
        )
        totals = rows.aggregate(total=Count("pk"), ahead=Count("pk", filter=ahead))
        top_points = list(
            rows.order_by("-summary_points", "summary_rank", "pk").values_list(
                "summary_points", flat=True
            )[:3]
        )
        completed = max(0, min(TOTAL_GAMEWEEKS, live_gw - 1))
        remaining = TOTAL_GAMEWEEKS - completed

        def project(score):
            pace = score / completed if completed else 50.0
            return int(round(score + remaining * pace))

        projected = project(points)
        projected_rank = totals["ahead"] + 1
        return {
            "league_id": mini_league.pk,
            "league_name": mini_league.name,
            "current_rank": rank,
            "projected_rank": projected_rank,
            "rank_change": rank - projected_rank,
            "projected_points": projected,
            "gap_to_leader": project(top_points[0]) - projected,
            "gap_to_podium": max(0, project(top_points[-1]) - projected),
            "total_teams": totals["total"],
        }
