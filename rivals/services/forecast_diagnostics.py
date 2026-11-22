"""Sufficient statistics for calibration, coverage and component diagnostics."""

from collections import defaultdict
from .player_forecast import PlayerForecast
from .historical_forecast_features import usable_history


def moments(predictions, actual, ids):
    ids = list(ids)
    return {
        "n": len(ids),
        "sum_predicted": sum(predictions[i] for i in ids),
        "sum_actual": sum(actual[i] for i in ids),
        "sum_predicted_squared": sum(predictions[i] ** 2 for i in ids),
        "sum_product": sum(predictions[i] * actual[i] for i in ids),
    }


def calibration(rows):
    if not rows:
        return {"n": 0, "slope": None, "intercept": None}
    keys = ("n", "sum_predicted", "sum_actual", "sum_predicted_squared", "sum_product")
    totals = {key: sum(row[key] for row in rows) for key in keys}
    n = totals["n"]
    denominator = (
        totals["sum_predicted_squared"] - totals["sum_predicted"] ** 2 / n if n else 0
    )
    slope = (
        (
            (totals["sum_product"] - totals["sum_predicted"] * totals["sum_actual"] / n)
            / denominator
        )
        if denominator > 1e-12
        else None
    )
    return {
        **totals,
        "predicted": totals["sum_predicted"] / n if n else None,
        "actual": totals["sum_actual"] / n if n else None,
        "slope": slope,
        "intercept": (
            (totals["sum_actual"] - slope * totals["sum_predicted"]) / n
            if slope is not None
            else None
        ),
    }


def groups_for_frame(players, fixtures, week, histories, history_usage=None):
    history = {int(pid): usable_history(rows) for pid, rows in histories.items()}
    schedule = PlayerForecast(players, fixtures, 0).fixtures
    groups = {
        "history": {"has_history": set(), "fallback": set()},
        "position": defaultdict(set),
        "fixture": defaultdict(set),
    }
    for p in players:
        pid = p["id"]
        saved = (history_usage or {}).get(str(pid), (history_usage or {}).get(pid))
        has_history = (
            saved["has_usable_history"] if saved is not None else bool(history.get(pid))
        )
        groups["history"]["has_history" if has_history else "fallback"].add(pid)
        groups["position"][str(p["element_type"])].add(pid)
        ratings = [
            f.get("team_h_difficulty" if home else "team_a_difficulty")
            for f, home in schedule.get((p["team"], week), [])
        ]
        # Doubles use the mean rating; missing ratings stay unknown.
        band = (
            "blank"
            if not ratings
            else (
                "unknown"
                if any(r not in (1, 2, 3, 4, 5) for r in ratings)
                else (
                    "easy"
                    if sum(ratings) / len(ratings) < 2.5
                    else "hard" if sum(ratings) / len(ratings) >= 3.5 else "neutral"
                )
            )
        )
        groups["fixture"][band].add(pid)
    return groups


def coverage(groups, considered):
    ids = set(considered)
    fallback = len(ids & groups["history"]["fallback"])
    return {
        "players": len(ids),
        "fallback_players": fallback,
        "fallback_share": fallback / len(ids) if ids else None,
    }
