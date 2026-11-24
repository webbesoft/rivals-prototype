"""Fixture-based scenarios, compared with an unchanged squad; no future data."""

import hashlib
import itertools
import json
import math
from collections import Counter, defaultdict

from django.core.cache import cache
from django.utils import timezone

from rivals.models import MiniLeagueTeam, TeamPlanningInput
from rivals.services.chip_advice_service import chip_advice
from rivals.services.fpl_api_service import FplApiService
from rivals.services.gameweek_context_service import GameweekContextService
from rivals.services.planning_defaults_service import PlanningDefaults
from rivals.services.active_player_forecast import build_player_forecast
from rivals.services.planned_lineup import replace_lineup, validate_lineup
from rivals.services.price_change_service import (
    PRICE_FIELDS,
    price_warning,
    transfer_timing,
)
from rivals.services.xi_benchmark import best_projected_xi

POSITIONS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
SQUAD_SHAPE = {1: 2, 2: 5, 3: 5, 4: 3}
FORMATIONS = [
    (d, m, 10 - d - m) for d in range(3, 6) for m in range(2, 6) if 1 <= 10 - d - m <= 3
]


class DecisionUnavailable(ValueError):
    pass


def number(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()


class DecisionEngine:
    VERSION = "decision-v10-planned-lineup"

    def __init__(self, user, api=None, *, fresh_squad=False):
        self.fresh_squad = fresh_squad
        self.user = user
        self.team = user.team
        self.api = api or FplApiService(snapshot_only=True)
        self.api.TIMEOUT = 4
        self._evaluations = {}

    def load(self):
        self.__dict__.pop("default_values", None)
        if not self.team:
            raise DecisionUnavailable("Add your FPL team to start planning.")
        self.bootstrap = self.api.fetch_bootstrap_static()
        self.fixtures = self.api.fetch_fixtures_data()
        if not self.bootstrap or self.fixtures is None:
            raise DecisionUnavailable(
                "We couldn't load the latest player and fixture details. Please try again."
            )
        self.context = GameweekContextService.derive(self.bootstrap.get("events", []))
        if (
            not self.context["season"]
            or not self.context["scoring_gameweek"]
            or not self.context["decision_gameweek"]
        ):
            raise DecisionUnavailable(
                "Transfer planning will be available when the next gameweek is announced and your lineup is available."
            )
        self.players = {
            p["id"]: p
            for p in self.bootstrap.get("elements", [])
            if p.get("element_type") in POSITIONS
        }
        self.club_names = {
            t["id"]: t.get("short_name", t.get("name"))
            for t in self.bootstrap.get("teams", [])
        }
        self.saved = TeamPlanningInput.objects.filter(
            user=self.user,
            team=self.team,
            season=self.context["season"],
            gameweek=self.context["decision_gameweek"],
        ).first()
        self.public_entry_history = {}
        source_week = self.context["scoring_gameweek"]
        while self.team.gameweek_data.filter(
            season=self.context["season"], gameweek=source_week, chip_name="freehit"
        ).exists():
            source_week -= 1
        if source_week < 1:
            raise DecisionUnavailable(
                "Your permanent squad is currently unavailable. Please try again."
            )
        rows = list(
            self.team.squad_histories.filter(
                season=self.context["season"], gameweek=source_week
            ).order_by("position")
        )
        if len(rows) == 15 and not self.fresh_squad:
            self.public_ids = [row.player_fpl_id for row in rows]
        else:
            picks = self.api.fetch_team_picks(self.team.fpl_team_id, source_week)
            self.public_entry_history = (picks or {}).get("entry_history", {})
            self.public_ids = [
                pick["element"] for pick in (picks or {}).get("picks", [])
            ]
        if len(self.public_ids) != 15 or any(
            pid not in self.players for pid in self.public_ids
        ):
            raise DecisionUnavailable(
                "Your complete squad is currently unavailable. Please try again."
            )
        self.fixture_index = defaultdict(list)
        for fixture in self.fixtures:
            if fixture.get("event") is not None and not fixture.get("finished"):
                self.fixture_index[(fixture.get("team_h"), fixture["event"])].append(
                    (fixture, True)
                )
                self.fixture_index[(fixture.get("team_a"), fixture["event"])].append(
                    (fixture, False)
                )
        source_fields = (
            "id",
            "web_name",
            "element_type",
            "team",
            "now_cost",
            "minutes",
            "form",
            "points_per_game",
            "status",
            "chance_of_playing_next_round",
            "news",
            "removed",
        ) + PRICE_FIELDS
        fixture_fields = (
            "event",
            "team_h",
            "team_a",
            "team_h_difficulty",
            "team_a_difficulty",
            "kickoff_time",
        )
        self.source_hash = digest(
            [
                [
                    {key: player.get(key) for key in source_fields}
                    for player in self.players.values()
                ],
                [
                    {key: fixture.get(key) for key in fixture_fields}
                    for fixture in self.fixtures
                    if fixture.get("event") is not None
                    and fixture["event"] >= self.context["decision_gameweek"]
                    and not fixture.get("finished")
                ],
                [
                    (event["id"], event.get("deadline_time"))
                    for event in self.bootstrap.get("events", [])
                ],
            ]
        )
        return self

    def name(self, pid):
        return self.players[pid].get("web_name") or str(pid)

    def validate_squad(self, ids):
        if (
            len(ids) != 15
            or len(set(ids)) != 15
            or any(pid not in self.players for pid in ids)
        ):
            raise ValueError("Choose 15 different players for your current squad.")
        if Counter(self.players[pid]["element_type"] for pid in ids) != SQUAD_SHAPE:
            raise ValueError(
                "Your squad needs 2 goalkeepers, 5 defenders, 5 midfielders and 3 forwards."
            )
        if max(Counter(self.players[pid]["team"] for pid in ids).values()) > 3:
            raise ValueError("Choose no more than 3 players from any club.")

    def inputs(self):
        record = self.saved
        previous = (
            record
            or TeamPlanningInput.objects.filter(user=self.user, team=self.team)
            .order_by("-updated_at")
            .first()
        )
        ids = (
            list(record.squad_ids)
            if record and record.squad_ids
            else list(self.public_ids)
        )
        if not hasattr(self, "default_values"):
            context = {**self.context, "events": self.bootstrap.get("events", [])}
            self.defaults_service = PlanningDefaults(
                self.user, context, self.players, self.public_entry_history
            )
            self.default_values = self.defaults_service.values(ids)
        lineup = record.planned_lineup if record else {}
        lineup_warning = ""
        try:
            validate_lineup(lineup, ids, self.players)
        except (ValueError, KeyError) as error:
            lineup = {}
            lineup_warning = f"Your saved XI could not be applied: {error} Using the optimised XI instead."
        return {
            "ids": ids,
            "confirmed": True,
            **self.default_values,
            "manual": list(record.manual_moves) if record else [],
            "lineup": lineup,
            "lineup_warning": lineup_warning,
            "horizon": previous.horizon if previous else 5,
            "strategy": previous.strategy if previous else "protect",
            "league_id": previous.target_league_id if previous else None,
            "rival_id": previous.target_rival_id if previous else None,
        }

    def prepare(self, inputs):
        self._evaluations = {}
        self._rival_weights = None
        self.inputs_used = inputs
        self.ids = list(inputs["ids"])
        self.validate_squad(self.ids)
        self.planned_lineup = inputs.get("lineup") or {}
        self.lineup_warning = inputs.get("lineup_warning", "")
        validate_lineup(self.planned_lineup, self.ids, self.players)
        self.weeks = list(
            range(
                self.context["decision_gameweek"],
                min(39, self.context["decision_gameweek"] + inputs["horizon"]),
            )
        )
        self.projections = {}
        self.risk = {}
        self.player_fixtures = {}
        self.forecast = build_player_forecast(
            self.players.values(),
            self.fixtures,
            self.context["scoring_gameweek"],
            teams=self.bootstrap.get("teams", []),
            season=self.context["season"],
        )
        self.projections = self.forecast.project(self.weeks)
        self.risk = {pid: self.forecast.risk(pid) for pid in self.players}
        for pid, player in self.players.items():
            self.player_fixtures[pid] = [
                {
                    "gameweek": week,
                    "opponents": [
                        f"{self.club_names.get(fixture['team_a'] if home else fixture['team_h'], '?')} ({'H' if home else 'A'})"
                        for fixture, home in self.fixture_index.get(
                            (player["team"], week), []
                        )
                    ],
                    "points": round(self.projections[pid][week], 1),
                }
                for week in self.weeks
            ]

    def fixture_difficulty_schedule(self, pid):
        """Five gameweeks of published FPL ratings, independent of forecast horizon."""
        start = self.context["decision_gameweek"]
        schedule = []
        for week in range(start, min(39, start + 5)):
            matches = []
            for fixture, home in self.fixture_index.get(
                (self.players[pid]["team"], week), []
            ):
                difficulty = number(
                    fixture.get("team_h_difficulty" if home else "team_a_difficulty")
                )
                opponent = fixture["team_a"] if home else fixture["team_h"]
                matches.append(
                    {
                        "opponent": self.club_names.get(opponent, "?"),
                        "venue": "H" if home else "A",
                        "difficulty": (
                            int(difficulty) if difficulty in (1, 2, 3, 4, 5) else None
                        ),
                    }
                )
            schedule.append({"gameweek": week, "matches": matches})
        return schedule

    def best_xi(self, week):
        """Share a captain-inclusive ceiling across managers and planner requests."""
        players = [
            {
                "id": pid,
                "team": player["team"],
                "element_type": player["element_type"],
                "expected_points": self.projections[pid][week],
            }
            for pid, player in sorted(self.players.items())
            if not player.get("removed", False)
        ]
        cache_key = f"planner_best_xi_v1_{digest([week, players])}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached or None
        benchmark = best_projected_xi(players, captaincy=True)
        cache.set(cache_key, benchmark or False, 900)
        return benchmark

    def evaluate(self, ids, *, lineup=None):
        # Never apply the manager's XI to a rival, even if they share all 15 players.
        if lineup is None:
            lineup = self.planned_lineup if set(ids) == set(self.ids) else {}
        key = (tuple(sorted(ids)), digest(lineup))
        if key in self._evaluations:
            return self._evaluations[key]
        groups = {
            position: [
                pid for pid in ids if self.players[pid]["element_type"] == position
            ]
            for position in POSITIONS
        }
        total, lineups, weekly_points = 0.0, [], {}
        for week in self.weeks:
            ordered = {
                pos: sorted(group, key=lambda pid: (-self.projections[pid][week], pid))
                for pos, group in groups.items()
            }
            best = None
            for defenders, midfielders, forwards in FORMATIONS:
                xi = (
                    ordered[1][:1]
                    + ordered[2][:defenders]
                    + ordered[3][:midfielders]
                    + ordered[4][:forwards]
                )
                captain = max(xi, key=lambda pid: (self.projections[pid][week], -pid))
                points = (
                    sum(self.projections[pid][week] for pid in xi)
                    + self.projections[captain][week]
                )
                if best is None or points > best[0]:
                    best = (points, xi, captain)
            points, xi, captain = best
            vice = max(
                (pid for pid in xi if pid != captain),
                key=lambda pid: (self.projections[pid][week], -pid),
            )
            if lineup and week == self.weeks[0]:
                xi = list(lineup["xi"])
                captain, vice = lineup["captain"], lineup["vice"]
                points = sum(self.projections[pid][week] for pid in xi)
                points += self.projections[captain][week]
            total += points
            weekly_points[week] = points
            lineups.append(
                {
                    "gameweek": week,
                    "xi": xi,
                    "captain": captain,
                    "vice": vice,
                    "points": round(points, 2),
                    "selection": (
                        "planned" if lineup and week == self.weeks[0] else "optimised"
                    ),
                }
            )
        result = {"points": total, "lineups": lineups, "weekly_points": weekly_points}
        self._evaluations[key] = result
        return result

    def candidate(self, moves, baseline):
        outgoing = [out for out, _ in moves]
        incoming = [new for _, new in moves]
        if len(set(outgoing)) != len(moves) or len(set(incoming)) != len(moves):
            return None
        if any(
            out not in self.ids or new in self.ids or new not in self.players
            for out, new in moves
        ):
            return None
        if any(
            self.players[out]["element_type"] != self.players[new]["element_type"]
            for out, new in moves
        ):
            return None
        ids = [pid for pid in self.ids if pid not in outgoing] + incoming
        try:
            self.validate_squad(ids)
        except ValueError:
            return None
        data = self.inputs_used
        exact_sales = all(str(out) in data["sales"] for out in outgoing)
        sale_value = sum(
            int(data["sales"].get(str(out), self.players[out].get("now_cost", 0)))
            for out in outgoing
        )
        purchase = sum(self.players[pid].get("now_cost", 0) for pid in incoming)
        bank = data["bank"]
        # Unknown selling prices are upper bounds, never confirmed spending power.
        if bank is not None and purchase > bank + sale_value:
            return None
        lineup = (
            replace_lineup(self.planned_lineup, moves) if self.planned_lineup else {}
        )
        projection = self.evaluate(ids, lineup=lineup)
        hits = (
            0
            if not moves
            else (
                4 * max(0, len(moves) - data["free_transfers"])
                if data["free_transfers"] is not None
                else None
            )
        )
        gross = projection["points"] - baseline["points"]
        first_points = projection["weekly_points"][self.weeks[0]]
        first_hold = baseline["weekly_points"][self.weeks[0]]
        net = gross - (hits if hits is not None else 4 * len(moves))
        unknown = []
        if not data["confirmed"]:
            unknown.append("Confirm your current squad")
        if bank is None:
            unknown.append("Confirm your bank balance")
        if hits is None:
            unknown.append("Confirm your free transfers")
        if not exact_sales:
            unknown.append("Confirm selling prices")
        after = self.lineup_weights(projection["lineups"])
        before = self.lineup_weights(baseline["lineups"])
        weights = {
            pid: after.get(pid, 0) - before.get(pid, 0)
            for pid in set(after) | set(before)
        }
        spread = math.sqrt(
            sum((weight * self.risk[pid]) ** 2 for pid, weight in weights.items())
        )
        risk_change = spread
        if self._rival_weights is not None:
            rival = self._rival_weights
            universe = set(after) | set(before) | set(rival)
            after_risk = math.sqrt(
                sum(
                    ((after.get(pid, 0) - rival.get(pid, 0)) * self.risk[pid]) ** 2
                    for pid in universe
                )
            )
            before_risk = math.sqrt(
                sum(
                    ((before.get(pid, 0) - rival.get(pid, 0)) * self.risk[pid]) ** 2
                    for pid in universe
                )
            )
            risk_change = after_risk - before_risk
        objective = (
            net + (-0.25 if data["strategy"] == "protect" else 0.25) * risk_change
        )
        return {
            "key": ",".join(f"{out}:{new}" for out, new in sorted(moves)) or "hold",
            "moves": [
                {
                    "out": out,
                    "in": new,
                    "out_name": self.name(out),
                    "in_name": self.name(new),
                    "buy_price": self.players[new].get("now_cost", 0) / 10,
                    "selling_price": (
                        data["sales"].get(str(out)) / 10
                        if str(out) in data["sales"]
                        else None
                    ),
                    "schedule": self.player_fixtures[new],
                    "news": self.players[new].get("news") or "",
                    "minutes_risk": self.risk[new] > 0.45,
                }
                for out, new in moves
            ],
            "ids": ids,
            "points": round(projection["points"], 1),
            "net_points": (
                round(projection["points"] - hits, 1) if hits is not None else None
            ),
            "immediate": {
                "gameweek": self.weeks[0],
                "points": round(first_points - hits, 1) if hits is not None else None,
                "hold_points": round(first_hold, 1),
                "net_gain": (
                    first_points - first_hold - hits if hits is not None else None
                ),
            },
            "gross_gain": round(gross, 1),
            "net_gain": round(gross - hits, 1) if hits is not None else None,
            "gain_low": round(
                gross - spread - (hits if hits is not None else 4 * len(moves)), 1
            ),
            "gain_high": round(gross + spread - (hits or 0), 1),
            "spread": round(spread, 1),
            "relative_risk_change": round(risk_change, 1),
            "hits": hits,
            "bank_after": (
                round((bank + sale_value - purchase) / 10, 1)
                if bank is not None
                else None
            ),
            "required_sales": round(max(0, purchase - (bank or 0)) / 10, 1),
            "unknown": unknown,
            "confirmed": not unknown,
            "free_next": (
                min(5, max(0, data["free_transfers"] - len(moves)) + 1)
                if data["free_transfers"] is not None
                else None
            ),
            "lineups": projection["lineups"],
            "lineup": lineup,
            "objective": objective,
        }

    def lineup_weights(self, lineups):
        weights = defaultdict(float)
        for line in lineups:
            for pid in line["xi"]:
                weights[pid] += self.projections[pid][line["gameweek"]]
            weights[line["captain"]] += self.projections[line["captain"]][
                line["gameweek"]
            ]
        return weights

    def options(self, baseline):
        opportunities = []
        owned = set(self.ids)
        for out in self.ids:
            pool = [
                pid
                for pid, player in self.players.items()
                if pid not in owned
                and player["element_type"] == self.players[out]["element_type"]
                and player.get("status", "a") in ("a", "d")
                and not player.get("removed", False)
                and sum(self.projections[pid].values()) > 0
            ]
            ordered = sorted(
                pool, key=lambda pid: (-sum(self.projections[pid].values()), pid)
            )
            immediate_ordered = sorted(
                pool, key=lambda pid: (-self.projections[pid][self.weeks[0]], pid)
            )
            # Include funding routes as well as the strongest individual forecasts.
            selected = (
                ordered[:4]
                + immediate_ordered[:2]
                + sorted(
                    pool,
                    key=lambda pid: (
                        self.players[pid].get("now_cost", 0),
                        -sum(self.projections[pid].values()),
                        pid,
                    ),
                )[:1]
            )
            if self.inputs_used["bank"] is not None:
                ceiling = self.inputs_used["bank"] + int(
                    self.inputs_used["sales"].get(
                        str(out), self.players[out].get("now_cost", 0)
                    )
                )
                selected += [
                    pid
                    for pid in ordered
                    if self.players[pid].get("now_cost", 0) <= ceiling
                ][:2]
                legal_immediate = next(
                    (
                        pid
                        for pid in immediate_ordered
                        if self.players[pid].get("now_cost", 0) <= ceiling
                        and self.candidate([(out, pid)], baseline) is not None
                    ),
                    None,
                )
                if legal_immediate is not None:
                    selected.append(legal_immediate)
            for incoming in dict.fromkeys(selected):
                opportunities.append((out, incoming))
        choices = [self.candidate([move], baseline) for move in opportunities]
        # Shortlists cover both immediate points and the full fixture window.
        for left, right in itertools.combinations(opportunities, 2):
            if left[0] != right[0] and left[1] != right[1]:
                choices.append(self.candidate([left, right], baseline))
        choices = [choice for choice in choices if choice is not None]
        choices.sort(
            key=lambda choice: (
                -choice["objective"],
                len(choice["moves"]),
                choice["key"],
            )
        )
        return choices

    def diverse_options(self, choices, primary=None, limit=5):
        """Show distinct problems first; variants belong under each squad slot."""
        eligible = [
            choice
            for choice in choices
            if (
                choice["net_gain"]
                if choice["net_gain"] is not None
                else choice["gross_gain"]
            )
            > 0
        ]
        eligible.sort(
            key=lambda item: (not item["confirmed"], -item["objective"], item["key"])
        )
        result, covered_players, covered_positions = [], set(), set()
        position_counts = Counter()

        def add(choice):
            result.append(choice)
            outgoing = {move["out"] for move in choice["moves"]}
            positions = {self.players[pid]["element_type"] for pid in outgoing}
            covered_players.update(outgoing)
            covered_positions.update(positions)
            position_counts.update(positions)

        if primary and primary["moves"]:
            add(primary)
        for seek_new_position in (True, False):
            for choice in eligible:
                if len(result) >= limit:
                    return result
                outgoing = {move["out"] for move in choice["moves"]}
                positions = {self.players[pid]["element_type"] for pid in outgoing}
                if outgoing & covered_players:
                    continue
                if seek_new_position and not positions - covered_positions:
                    continue
                if any(position_counts[position] >= 2 for position in positions):
                    continue
                add(choice)
        return result

    def rival_context(self, data, baseline):
        memberships = self.user.customminileagues.filter(
            active=True, mini_league__active=True
        )
        league = (
            memberships.filter(mini_league_id=data["league_id"])
            .select_related("mini_league")
            .first()
            if data["league_id"]
            else memberships.select_related("mini_league").first()
        )
        if not league:
            return None
        if league.mini_league.uses_summary:
            return {"league": league.mini_league.name, "unavailable": True}
        standings = MiniLeagueTeam.objects.filter(
            mini_league=league.mini_league, season=self.context["season"]
        ).select_related("team")
        mine = standings.filter(team=self.team).first()
        rival = (
            standings.filter(team_id=data["rival_id"]).first()
            if data["rival_id"]
            else standings.exclude(team=self.team)
            .order_by("current_rank", "pk")
            .first()
        )
        if not rival:
            return {"league": league.mini_league.name, "unavailable": True}
        rows = list(
            rival.team.squad_histories.filter(
                season=self.context["season"], gameweek=self.context["scoring_gameweek"]
            ).order_by("position")
        )
        if len(rows) != 15 or any(
            row.player_fpl_id not in self.players for row in rows
        ):
            return {
                "league": league.mini_league.name,
                "rival": rival.team.team_name,
                "gap": rival.total_points - mine.total_points if mine else None,
                "unavailable": True,
            }
        rival_ids = [row.player_fpl_id for row in rows]
        try:
            self.validate_squad(rival_ids)
        except ValueError:
            return {
                "league": league.mini_league.name,
                "rival": rival.team.team_name,
                "unavailable": True,
            }
        projection = self.evaluate(rival_ids, lineup={})
        shared = set(self.ids) & set(rival_ids)
        differentials = []
        for side, ids in (
            ("You", set(self.ids) - shared),
            ("Rival", set(rival_ids) - shared),
        ):
            for pid in ids:
                differentials.append(
                    {
                        "side": side,
                        "name": self.name(pid),
                        "points": round(sum(self.projections[pid].values()), 1),
                        "id": pid,
                    }
                )
        differentials.sort(key=lambda item: (-item["points"], item["id"]))
        captain = projection["lineups"][0]["captain"]
        return {
            "league": league.mini_league.name,
            "rival": rival.team.team_name,
            "gap": rival.total_points - mine.total_points if mine else None,
            "shared": len(shared),
            "different_players": differentials[:10],
            "hold_gap": round(baseline["points"] - projection["points"], 1),
            "rival_points": round(projection["points"], 1),
            "ids": rival_ids,
            "captain_name": self.name(captain),
            "captain_id": captain,
            "hold_captain_swing": round(
                2
                * (
                    self.projections[baseline["lineups"][0]["captain"]][self.weeks[0]]
                    - self.projections[captain][self.weeks[0]]
                ),
                1,
            ),
            "updated_at": league.mini_league.last_synced_at,
        }

    def rival_signature(self, data):
        league_ids = list(
            self.user.customminileagues.filter(active=True, mini_league__active=True)
            .order_by("mini_league_id")
            .values_list("mini_league_id", flat=True)
        )
        league_id = (
            data["league_id"]
            if data["league_id"] in league_ids
            else (league_ids[0] if league_ids and not data["league_id"] else None)
        )
        standings = MiniLeagueTeam.objects.filter(
            mini_league_id=league_id, season=self.context["season"]
        ).select_related("team")
        mine = standings.filter(team=self.team).first()
        rival = (
            standings.filter(team_id=data["rival_id"]).first()
            if data["rival_id"]
            else standings.exclude(team=self.team)
            .order_by("current_rank", "pk")
            .first()
        )
        scores = [
            (row.team_id, row.current_rank, row.total_points)
            for row in (mine, rival)
            if row
        ]
        squad = (
            list(
                rival.team.squad_histories.filter(
                    season=self.context["season"],
                    gameweek=self.context["scoring_gameweek"],
                )
                .order_by("player_fpl_id")
                .values_list("player_fpl_id", flat=True)
            )
            if rival
            else []
        )
        return [league_ids, scores, squad]

    def player_forecasts(self):
        return [
            {
                "id": pid,
                "name": self.name(pid),
                "position": POSITIONS[self.players[pid]["element_type"]],
                "element_type": self.players[pid]["element_type"],
                "club": self.club_names.get(self.players[pid]["team"], "—"),
                "news": self.players[pid].get("news", ""),
                "price_warning": price_warning(self.players[pid]),
                "risk_label": (
                    "Check news"
                    if self.players[pid].get("status", "a") not in ("a", "d")
                    or number(
                        self.players[pid].get("chance_of_playing_next_round"), 100
                    )
                    < 100
                    else "Minutes risk" if self.risk[pid] > 0.45 else ""
                ),
                "schedule": self.player_fixtures[pid],
                "minutes_risk": round(self.risk[pid], 2),
                "price": self.players[pid].get("now_cost", 0) / 10,
            }
            for pid in self.ids
        ]

    def player_outlooks(self):
        """Explain fixture changes and observed form separately; never infer recovery."""
        outlooks = []
        for pid in self.ids:
            player = self.players[pid]
            form = number(player.get("form"))
            season_average = number(player.get("points_per_game"))
            first = self.projections[pid][self.weeks[0]]
            later = [self.projections[pid][week] for week in self.weeks[1:]]
            later_average = sum(later) / len(later) if later else None
            improves = later_average is not None and later_average > max(
                first * 1.15, first + 0.5
            )
            weak_form = season_average > 0 and form < season_average * 0.75
            availability_risk = (
                player.get("status", "a") not in ("a", "d")
                or number(player.get("chance_of_playing_next_round"), 100) < 100
                or self.risk[pid] > 0.45
            )
            if not (improves or weak_form or availability_risk):
                continue
            labels, details = [], []
            if availability_risk:
                labels.append("Availability or minutes concern")
                details.append(
                    "Playing time lowers this estimate. Current availability is held "
                    "constant across the window; no recovery date is assumed."
                )
            if weak_form:
                labels.append("Recent form below season average")
                details.append(
                    f"FPL form {form:.1f} versus {season_average:.1f} points per game "
                    "this season. "
                    + (
                        "The revised estimate uses underlying performance and expected minutes; this flag describes observed FPL returns."
                        if self.forecast.experimental
                        else "The forecast carries this form forward; a sustained rebound is not assumed."
                    )
                )
            if improves:
                labels.append("Fixtures improve")
                details.append(
                    f"GW{self.weeks[0]}: {first:.1f} projected player points; "
                    f"GW{self.weeks[1]}–{self.weeks[-1]}: {later_average:.1f} per "
                    "gameweek on average. The schedule lifts the estimate. "
                    "Consider that improvement before selling for this week alone."
                )
            outlooks.append(
                {
                    "id": pid,
                    "name": self.name(pid),
                    "label": "; ".join(labels),
                    "detail": " ".join(details),
                    "fixtures_improve": improves,
                    "weak_form": weak_form,
                    "availability_risk": availability_risk,
                }
            )
        return outlooks

    def weekly_comparison(self, choice, baseline):
        """Charge this deadline's hit once, in the first gameweek."""
        comparison, cumulative = [], 0.0
        projected = self.evaluate(choice["ids"], lineup=choice.get("lineup", {}))[
            "weekly_points"
        ]
        for index, line in enumerate(choice["lineups"]):
            week = line["gameweek"]
            points, hold_points = projected[week], baseline["weekly_points"][week]
            hit = choice["hits"] if index == 0 else 0
            gain = points - hold_points - (hit or 0)
            cumulative += gain
            comparison.append(
                {
                    "gameweek": line["gameweek"],
                    "points": (round(points - hit, 1) if hit is not None else None),
                    "hold_points": round(hold_points, 1),
                    "hits": hit,
                    "net_gain": round(gain, 1) if hit is not None else None,
                    "cumulative_gain": (
                        round(cumulative, 1) if choice["hits"] is not None else None
                    ),
                }
            )
        return comparison

    def preview(self):
        """Forecast the unchanged squad without searching transfer combinations."""
        self.prepare(self.inputs())
        return {
            "gameweek": self.weeks[0],
            "deadline": self.context["next_deadline"].isoformat(),
            "baseline": self.evaluate(self.ids),
            "benchmarks": {week: self.best_xi(week) for week in self.weeks},
            "players": self.player_forecasts(),
            "lineup_mode": "planned" if self.planned_lineup else "optimised",
            "lineup_warning": self.lineup_warning,
            "model": self.model_info(),
        }

    def model_info(self):
        return {
            "version": self.VERSION,
            "player_model": self.forecast.version,
            "label": self.forecast.display_name,
            "experimental": self.forecast.experimental,
            "method": self.forecast.method
            + " Availability stays constant; blanks score zero and doubles count each match. "
            + (
                "Your planned XI and captain are kept for the next gameweek; incoming players inherit the outgoing player's place and armband. Later gameweeks select the best legal XI and captain. "
                if self.planned_lineup
                else "Best legal XI and captain are selected separately each week. "
            )
            + "Ranges are sensitivity estimates, not confidence intervals. No chips or automatic substitutions are assumed.",
        }

    def chip_usage(self):
        first = 1 if self.weeks[0] <= 19 else 20
        return dict(
            self.team.gameweek_data.filter(
                season=self.context["season"],
                gameweek__gte=first,
                gameweek__lte=self.weeks[0],
            )
            .exclude(chip_name__isnull=True)
            .exclude(chip_name="")
            .order_by("gameweek")
            .values_list("chip_name", "gameweek")
        )

    def report(self, inputs=None, manual=None):
        if not hasattr(self, "players"):
            self.load()
        data = inputs or self.inputs()
        self.prepare(data)
        manual = manual if manual is not None else data.get("manual", [])
        if len(manual) > 2:
            raise ValueError("Compare at most two transfers at this deadline.")
        fingerprint = digest(
            [
                self.VERSION,
                self.source_hash,
                self.forecast.cache_identity,
                self.chip_usage(),
                self.rival_signature(data),
                self.context["season"],
                self.weeks,
                data,
                manual,
            ]
        )
        cache_key = f"decision_{self.user.pk}_{self.team.pk}_{fingerprint}_pitch2"
        cached = cache.get(cache_key)
        if cached:
            return cached
        baseline = self.evaluate(self.ids)
        hold = self.candidate([], baseline)
        hold["unknown"] = [] if data["confirmed"] else ["Confirm your current squad"]
        hold["confirmed"] = data["confirmed"]
        # Holding has no hit or budget requirement, including when transfer allowance is unknown.
        hold.update(hits=0, net_gain=0.0, gain_low=0.0, gain_high=0.0, objective=0.0)
        rival = self.rival_context(data, baseline)
        if rival and "ids" in rival:
            self._rival_weights = self.lineup_weights(
                self.evaluate(rival["ids"], lineup={})["lineups"]
            )
        choices = self.options(baseline)
        manual_option = self.candidate(manual, baseline) if manual else None
        if manual and manual_option is None:
            raise ValueError(
                "That plan breaks a squad rule or exceeds your budget. Review the players and prices."
            )
        if manual_option:
            choices.append(manual_option)
            choices.sort(key=lambda item: (-item["objective"], item["key"]))
        choices = sorted(
            {choice["key"]: choice for choice in choices}.values(),
            key=lambda item: (-item["objective"], item["key"]),
        )
        qualified = [
            choice
            for choice in choices
            if choice["net_gain"] is not None
            and choice["net_gain"] > 0
            and choice["objective"] > 0
        ]
        best = qualified[0] if qualified else hold
        immediate_choices = [
            choice
            for choice in choices
            if choice["immediate"]["net_gain"] is not None
            and choice["immediate"]["net_gain"] > 0
        ]
        short_term = (
            min(
                immediate_choices,
                key=lambda item: (
                    -item["immediate"]["net_gain"],
                    -item["objective"],
                    len(item["moves"]),
                    item["key"],
                ),
            )
            if immediate_choices
            else hold
        )
        top = self.diverse_options(choices, primary=best if best["moves"] else None)
        if manual_option and all(item["key"] != manual_option["key"] for item in top):
            top.insert(0, manual_option)
        for choice in [hold, short_term] + top:
            choice["vs_rival"] = (
                round(
                    choice["points"] - (choice["hits"] or 0) - rival["rival_points"], 1
                )
                if rival and "rival_points" in rival and choice["hits"] is not None
                else None
            )
        risks = []
        for pid in self.ids:
            player = self.players[pid]
            availability = number(player.get("chance_of_playing_next_round"), 100)
            if (
                player.get("status", "a") not in ("a", "d")
                or availability < 100
                or self.risk[pid] > 0.45
            ):
                risks.append(
                    {
                        "name": self.name(pid),
                        "news": player.get("news")
                        or "Playing time or availability needs attention.",
                        "id": pid,
                    }
                )
        first = baseline["lineups"][0]
        single_choices = [choice for choice in choices if len(choice["moves"]) == 1]
        slot_leaders = []
        for outgoing in self.ids:
            single = [
                choice
                for choice in single_choices
                if choice["moves"][0]["out"] == outgoing
            ]
            if single:
                slot_leaders.append(single[0])
        alternative_leaders = self.diverse_options(slot_leaders)
        alternatives = [
            {
                "player": self.name(leader["moves"][0]["out"]),
                "position": POSITIONS[
                    self.players[leader["moves"][0]["out"]]["element_type"]
                ],
                "options": [
                    choice
                    for choice in single_choices
                    if choice["moves"][0]["out"] == leader["moves"][0]["out"]
                ][:3],
            }
            for leader in alternative_leaders
        ]
        scenarios = [hold] + top
        displayed = (
            scenarios
            + [best, short_term]
            + [choice for slot in alternatives for choice in slot["options"]]
        )
        outlooks = self.player_outlooks()
        outlook_by_id = {item["id"]: item for item in outlooks}
        for choice in {choice["key"]: choice for choice in displayed}.values():
            choice["weekly"] = self.weekly_comparison(choice, baseline)
            choice["price_notes"] = transfer_timing(choice, self.players)
            for move in choice["moves"]:
                outgoing, incoming = move["out"], move["in"]
                move.update(
                    out_schedule=self.player_fixtures[outgoing],
                    out_fdr_schedule=self.fixture_difficulty_schedule(outgoing),
                    in_fdr_schedule=self.fixture_difficulty_schedule(incoming),
                    out_next_points=round(self.projections[outgoing][self.weeks[0]], 1),
                    in_next_points=round(self.projections[incoming][self.weeks[0]], 1),
                    out_points=round(sum(self.projections[outgoing].values()), 1),
                    in_points=round(sum(self.projections[incoming].values()), 1),
                    outlook=outlook_by_id.get(outgoing),
                )
        result = {
            "fingerprint": fingerprint,
            "season": self.context["season"],
            "gameweek": self.weeks[0],
            "scoring_gameweek": self.context["scoring_gameweek"],
            "deadline": self.context["next_deadline"].isoformat(),
            "horizon": len(self.weeks),
            "end_gameweek": self.weeks[-1],
            "created_at": timezone.now().isoformat(),
            "strategy": data["strategy"],
            "baseline": hold,
            "benchmarks": {week: self.best_xi(week) for week in self.weeks},
            "best": best,
            "short_term": short_term,
            "short_term_is_separate": all(
                short_term["key"] != choice["key"] for choice in scenarios
            ),
            "options": scenarios,
            "manual": manual_option,
            "alternatives": alternatives[:5],
            "risks": risks[:5],
            "lineup_mode": "planned" if self.planned_lineup else "optimised",
            "lineup_warning": self.lineup_warning,
            "price_warnings": [
                {"id": pid, "name": self.name(pid), **warning}
                for pid in self.ids
                if (warning := price_warning(self.players[pid]))
            ],
            "chip_advice": chip_advice(self, baseline),
            "rival": rival,
            "captain": (
                self.name(first["captain"])
                if self.planned_lineup
                or self.projections[first["captain"]][self.weeks[0]] > 0
                else None
            ),
            "vice": (
                self.name(first["vice"])
                if self.planned_lineup
                or self.projections[first["vice"]][self.weeks[0]] > 0
                else None
            ),
            "captain_id": first["captain"],
            "vice_id": first["vice"],
            "players": self.player_forecasts(),
            "player_outlooks": outlooks,
            "inputs": data,
            "bank_display": data["bank"] / 10 if data["bank"] is not None else 0,
            "weeks": self.weeks,
            "model": self.model_info(),
        }
        cache.set(cache_key, result, 120)
        return result
