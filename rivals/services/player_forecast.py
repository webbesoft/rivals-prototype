"""The shared, pure player-points model; consumers own selection constraints."""

import math
from collections import defaultdict

MODEL_VERSION = "heuristic-v1"


def number(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


class PlayerForecast:
    version = MODEL_VERSION

    def __init__(self, players, fixtures, scoring_gameweek):
        self.players = {p["id"]: p for p in players}
        self.scoring_gameweek = scoring_gameweek or 0
        self.fixtures = defaultdict(list)
        for fixture in fixtures:
            if fixture.get("event") is not None and not fixture.get("finished"):
                self.fixtures[fixture["team_h"], fixture["event"]].append(
                    (fixture, True)
                )
                self.fixtures[fixture["team_a"], fixture["event"]].append(
                    (fixture, False)
                )

    def factors(self, player):
        availability = min(
            1, max(0, number(player.get("chance_of_playing_next_round"), 100) / 100)
        )
        if player.get("status", "a") not in ("a", "d"):
            availability = 0.0
        minutes = min(
            1,
            max(0, number(player.get("minutes")) / max(90, self.scoring_gameweek * 70)),
        )
        base = max(
            0,
            0.6 * number(player.get("form"))
            + 0.4 * number(player.get("points_per_game")),
        )
        return base, minutes, availability

    def risk(self, pid):
        _, minutes, availability = self.factors(self.players[pid])
        return 0.25 + (1 - minutes * availability) * 0.5

    def points(self, pid, week):
        player = self.players[pid]
        base, minutes, availability = self.factors(player)
        points = 0.0
        for fixture, home in self.fixtures.get((player["team"], week), []):
            difficulty = number(
                fixture.get("team_h_difficulty" if home else "team_a_difficulty"), 3
            )
            factor = max(
                0.6, min(1.4, 1 + (3 - difficulty) * 0.12 + (0.04 if home else -0.04))
            )
            points += base * factor * minutes * availability
        return points

    def project(self, weeks):
        return {
            pid: {week: self.points(pid, week) for week in weeks}
            for pid in self.players
        }
