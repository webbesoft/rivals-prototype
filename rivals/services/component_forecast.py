"""Experimental component model. Priors are explicit hypotheses, not fitted weights."""

import math

from .player_forecast import PlayerForecast, number
from .forecast_parameters import COMPONENT_CONFIG
from .historical_forecast_features import usable_history


def poisson_floor(lam, divisor):
    """E[floor(X/divisor)] for Poisson X (integer scoring thresholds)."""
    probability = math.exp(-lam)
    result = 0.0
    for count in range(1, 100):
        probability *= lam / count
        result += (count // divisor) * probability
    return result


class ComponentForecast(PlayerForecast):
    version = "decision-v11"
    # Per-90 priors by position, with 900 minutes of shrinkage.
    priors = {1: (0.005, 0.01), 2: (0.06, 0.10), 3: (0.20, 0.18), 4: (0.35, 0.12)}

    def __init__(
        self,
        players,
        fixtures,
        scoring_gameweek,
        *,
        teams=(),
        histories=None,
        season="2026/27",
    ):
        super().__init__(players, fixtures, scoring_gameweek)
        self.season_year = int(season[:4])
        self.teams = {t["id"]: t for t in teams}
        self.histories = {
            int(pid): sorted(
                usable_history(rows),
                key=lambda row: (
                    row.get("kickoff_time") or "",
                    number(row.get("fixture")),
                ),
            )
            for pid, rows in (histories or {}).items()
        }
        self.config = dict(COMPONENT_CONFIG)

    def minutes(self, pid, offset=0, double=False):
        p = self.players[pid]
        _, _, availability = self.factors(p)
        historical_proxy = p.get("availability_source") == "recent-match-proxy"
        # Historical P(play) already comes from these same recent minutes. Do not
        # multiply it twice; apply the common proxy as a cap instead.
        proxy = availability
        if historical_proxy:
            availability = 1.0
        history = self.histories.get(pid, [])[-6:]
        season_mean = min(90, number(p.get("minutes")) / max(1, self.scoring_gameweek))
        if history:
            weights = [
                self.config["recency_weight"] ** (len(history) - 1 - i)
                for i in range(len(history))
            ]
            denominator = sum(weights)
            avg = (
                sum(number(h.get("minutes")) * w for h, w in zip(history, weights))
                / denominator
            )
            play = (
                sum(
                    (number(h.get("minutes")) > 0) * w for h, w in zip(history, weights)
                )
                / denominator
            )
            sixty = (
                sum(
                    (number(h.get("minutes")) >= 60) * w
                    for h, w in zip(history, weights)
                )
                / denominator
            )
            start = (
                sum(
                    (number(h.get("starts"), number(h.get("minutes")) >= 60) > 0) * w
                    for h, w in zip(history, weights)
                )
                / denominator
            )
        else:
            avg = season_mean
            play = min(1, season_mean / 60)
            sixty = min(play, season_mean / 90)
            start = sixty
        # Later weeks gradually regress recent minutes toward season usage.
        decay = self.config["horizon_decay"] ** offset
        avg = decay * avg + (1 - decay) * season_mean
        play = decay * play + (1 - decay) * min(1, season_mean / 60)
        sixty = decay * sixty + (1 - decay) * min(1, season_mean / 90)
        if historical_proxy:
            avg = min(avg, 90 * proxy)
            play, sixty, start = min(play, proxy), min(sixty, proxy), min(start, proxy)
        rotation = self.config["double_rotation"] if double else 1.0
        return {
            "expected": min(90, max(0, avg)) * availability * rotation,
            "play": play * availability * rotation,
            "sixty": sixty * availability * rotation,
            "start": start * availability * rotation,
            "source": "recent-six" if history else "season-fallback",
        }

    def strength(self, team, field):
        values = [
            number(t.get(field))
            for t in self.teams.values()
            if number(t.get(field)) > 0
        ]
        average = sum(values) / len(values) if values else 1
        value = number(self.teams.get(team, {}).get(field), average)
        return max(0.5, min(2, value / average)) if value > 0 else 1

    def components(self, pid, week):
        p = self.players[pid]
        position = p["element_type"]
        fixtures = self.fixtures.get((p["team"], week), [])
        offset = max(0, week - (self.scoring_gameweek + 1))
        minutes = self.minutes(pid, offset, len(fixtures) > 1)
        played = max(0, number(p.get("minutes")))
        goal_prior, assist_prior = self.priors[position]
        history = self.histories.get(pid, [])

        def attacking_rate(field, prior):
            total = number(p.get(field), prior * played / 90)
            season_rate = (total * 90 + self.config["season_prior_minutes"] * prior) / (
                played + self.config["season_prior_minutes"]
            )
            recent = history[-6:]
            if not recent or not any(h.get(field) not in (None, "") for h in recent):
                return season_rate
            weights = [
                self.config["recency_weight"] ** (len(recent) - 1 - i)
                for i in range(len(recent))
            ]
            recent_minutes = sum(
                number(h.get("minutes")) * w for h, w in zip(recent, weights)
            )
            recent_stat = sum(number(h.get(field)) * w for h, w in zip(recent, weights))
            recent_rate = (
                90 * recent_stat + self.config["recent_prior_minutes"] * season_rate
            ) / (recent_minutes + self.config["recent_prior_minutes"])
            decay = self.config["horizon_decay"] ** offset
            return decay * recent_rate + (1 - decay) * season_rate

        xg90 = attacking_rate("expected_goals", goal_prior)
        xa90 = attacking_rate("expected_assists", assist_prior)
        history_minutes = sum(number(h.get("minutes")) for h in history)

        def rate(field, prior):
            return (
                sum(number(h.get(field)) for h in history) * 90
                + self.config["season_prior_minutes"] * prior
            ) / (history_minutes + self.config["season_prior_minutes"])

        bonus90, yellow90, red90 = (
            rate("bonus", 0.2),
            rate("yellow_cards", 0.15),
            rate("red_cards", 0.01),
        )
        totals = dict.fromkeys(
            (
                "appearance",
                "goals",
                "assists",
                "clean_sheet",
                "saves",
                "bonus",
                "cards",
                "conceded",
                "defensive_contribution",
                "penalties",
                "own_goals",
            ),
            0.0,
        )
        variance = 0.0
        for fixture, home in fixtures:
            venue, opposite = ("home", "away") if home else ("away", "home")
            opponent = fixture["team_a"] if home else fixture["team_h"]
            attack = self.strength(
                p["team"], f"strength_attack_{venue}"
            ) / self.strength(opponent, f"strength_defence_{opposite}")
            conceded = (
                (1.2 if home else 1.5)
                * self.strength(opponent, f"strength_attack_{opposite}")
                / self.strength(p["team"], f"strength_defence_{venue}")
            )
            exposure = minutes["expected"] / 90
            goals, assists = xg90 * exposure * attack, xa90 * exposure * attack
            goal_value, cs_value = {
                1: (10 if self.season_year >= 2024 else 6, 4),
                2: (6, 4),
                3: (5, 1),
                4: (4, 0),
            }[position]
            cs_probability = math.exp(-conceded) * minutes["sixty"]
            totals["appearance"] += minutes["play"] + minutes["sixty"]
            totals["goals"] += goals * goal_value
            totals["assists"] += assists * 3
            totals["clean_sheet"] += cs_probability * cs_value
            # Threshold scoring is conditional on playing. Availability is a
            # mixture of appearances and non-appearances, not a thinner Poisson.
            play = minutes["play"]
            conditional_exposure = exposure / play if play > 0 else 0.0
            totals["saves"] += (
                play * poisson_floor(rate("saves", 3) * conditional_exposure, 3)
                if position == 1
                else 0
            )
            totals["bonus"] += bonus90 * exposure
            totals["cards"] -= (yellow90 + 3 * red90) * exposure
            totals["conceded"] -= (
                play * poisson_floor(conceded * conditional_exposure, 2)
                if position in (1, 2)
                else 0
            )
            totals["penalties"] += (
                5 * rate("penalties_saved", 0) - 2 * rate("penalties_missed", 0)
            ) * exposure
            totals["own_goals"] -= 2 * rate("own_goals", 0) * exposure
            if self.season_year >= 2025 and position != 1:
                threshold = 10 if position == 2 else 12
                prior = 0.15 if position == 2 else 0.05
                eligible_history = [h for h in history if number(h.get("minutes")) > 0]
                successes = sum(
                    number(h.get("defensive_contribution")) >= threshold
                    for h in eligible_history
                )
                probability = (successes + 5 * prior) / (len(eligible_history) + 5)
                totals["defensive_contribution"] += 2 * probability * minutes["play"]
            variance += (
                goals * goal_value**2
                + assists * 9
                + cs_value**2 * cs_probability * (1 - cs_probability)
            )
        return {
            "points": sum(totals.values()),
            "components": totals,
            "minutes": minutes,
            "variance_proxy": variance,
            "variance_calibrated": False,
        }

    def points(self, pid, week):
        return self.components(pid, week)["points"]
