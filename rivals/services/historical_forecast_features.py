"""Lag-only historical fixture/availability proxies shared by model challengers."""

import math
from collections import defaultdict

from .player_forecast import number
from .forecast_parameters import COMPONENT_CONFIG


def recent_play_probability(history):
    recent = sorted(
        history, key=lambda r: (r.get("kickoff_time", ""), int(r.get("fixture") or 0))
    )[-6:]
    if not recent:
        return 1.0
    weights = [
        COMPONENT_CONFIG["recency_weight"] ** (len(recent) - 1 - i)
        for i in range(len(recent))
    ]
    return sum(
        (number(row.get("minutes")) > 0) * w for row, w in zip(recent, weights)
    ) / sum(weights)


def rolling_strengths(prior_rows, clubs):
    """Last ten completed team matches, five venue-prior pseudo-matches.

    Deduplicate player rows by team/fixture. Caller enforces GW and time cutoffs.
    Scores are team goals, never the sum of player goals or duplicated team rows.
    """
    matches = defaultdict(dict)
    for row in prior_rows:
        if row.get("team_h_score") in (None, "") or row.get("team_a_score") in (
            None,
            "",
        ):
            continue
        home = str(row.get("was_home")).lower() == "true"
        gf = number(row["team_h_score"] if home else row["team_a_score"])
        ga = number(row["team_a_score"] if home else row["team_h_score"])
        matches[row.get("team")][row.get("fixture")] = (
            row["kickoff_time"],
            home,
            gf,
            ga,
        )
    result = []
    for club in sorted(clubs):
        recent = sorted(matches[club].values())[-10:]
        team = {
            "id": club,
            "strength_source": "prior-completed-team-goals",
            "matches": len(recent),
        }
        for venue, home in (("home", True), ("away", False)):
            own = [m for m in recent if m[1] == home]
            prior_gf, prior_ga = (1.5, 1.2) if home else (1.2, 1.5)
            gf = (sum(m[2] for m in own) + 5 * prior_gf) / (len(own) + 5)
            ga = (sum(m[3] for m in own) + 5 * prior_ga) / (len(own) + 5)
            team[f"strength_attack_{venue}"] = gf / prior_gf
            team[f"strength_defence_{venue}"] = prior_ga / ga
        result.append(team)
    return result


def fixture_proxies(fixtures, teams):
    """Legacy FDR-shaped proxy from the same rolling strengths used by components."""
    teams = {t["id"]: t for t in teams}
    for fixture in fixtures:
        for home in (True, False):
            opponent = fixture["team_a"] if home else fixture["team_h"]
            venue = "away" if home else "home"
            strength = teams.get(opponent, {})
            overall = math.sqrt(
                strength.get(f"strength_attack_{venue}", 1)
                * strength.get(f"strength_defence_{venue}", 1)
            )
            fixture["team_h_difficulty" if home else "team_a_difficulty"] = max(
                1, min(5, round(3 + 2 * (overall - 1)))
            )
    return fixtures


def usable_history(rows):
    return [
        row
        for row in rows
        if math.isfinite(number(row.get("minutes"), float("nan")))
        and 0 <= number(row.get("minutes")) <= 130
    ]
