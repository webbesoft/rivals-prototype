"""Offline feature ablations on identical, forecast-selected candidate cohorts.

These interventions never change the deployed model or its live fingerprint.
"""

import hashlib
import math
from collections import defaultdict
from statistics import mean

from .component_forecast import ComponentForecast
from .forecast_backtest import validate_frame
from .forecast_diagnostics import calibration, groups_for_frame, moments
from .forecast_evaluation import (
    aggregate_candidates,
    bootstrap_paired,
    candidate_scores,
    evaluate,
    summarise_pairs,
    top_projected,
)
from .forecast_parameters import model_fingerprint
from .player_forecast import PlayerForecast, number

REFERENCE = PlayerForecast.version
FULL = ComponentForecast.version


class _SeasonMinutesForecast(ComponentForecast):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._season_usage = ComponentForecast(
            self.players.values(),
            (),
            self.scoring_gameweek,
            season=kwargs["season"],
        )

    def minutes(self, pid, offset=0, double=False):
        return self._season_usage.minutes(pid, offset, double)


def validation_predictions(frame):
    """Return predictions and the usable-history IDs actually used per variant."""
    players, fixtures, scoring = (
        frame["players"],
        frame["fixtures"],
        frame["scoring_gameweek"],
    )
    histories = {int(k): v for k, v in frame.get("histories", {}).items()}
    underlying = {"expected_goals", "expected_assists"}

    def without_stats(rows):
        return [{k: v for k, v in row.items() if k not in underlying} for row in rows]

    stripped_history = {pid: without_stats(rows) for pid, rows in histories.items()}
    half_history = {
        pid: rows
        for pid, rows in histories.items()
        if int(hashlib.sha256(f"{frame['season']}:{pid}".encode()).hexdigest(), 16) % 2
    }

    def component(*, pool=players, teams=None, history=None, cls=ComponentForecast):
        return cls(
            pool,
            fixtures,
            scoring,
            teams=frame.get("teams", []) if teams is None else teams,
            histories=histories if history is None else history,
            season=frame["season"],
        )

    models = {
        REFERENCE: PlayerForecast(players, fixtures, scoring),
        FULL: component(),
        "without-team-strengths": component(teams=[]),
        "without-xg-xa": component(
            pool=without_stats(players), history=stripped_history
        ),
        "season-attacking-rates": component(history=stripped_history),
        "season-minutes": component(cls=_SeasonMinutesForecast),
        "half-history-fallback": component(history=half_history),
        "all-history-fallback": component(history={}),
    }
    predictions = {
        name: {p["id"]: model.points(p["id"], frame["gameweek"]) for p in players}
        for name, model in models.items()
    }
    usage = {
        name: {pid for pid, rows in model.histories.items() if rows}
        for name, model in models.items()
        if isinstance(model, ComponentForecast)
    }
    usage[REFERENCE] = usage[FULL]
    return predictions, usage


def _coverage(ids, used):
    n = len(ids)
    fallback = len(set(ids) - used)
    return {
        "players": n,
        "fallback_players": fallback,
        "fallback_share": fallback / n if n else None,
    }


def _week(frame, top_ks, prediction_fn):
    validate_frame(frame)
    actual = {int(k): number(v, float("nan")) for k, v in frame["actual"].items()}
    if any(
        p["id"] not in actual or not math.isfinite(actual[p["id"]])
        for p in frame["players"]
    ):
        raise ValueError(
            "Complete finite scores are required for the forecast player pool"
        )
    predictions, usage = prediction_fn(frame)
    groups = groups_for_frame(
        frame["players"],
        frame["fixtures"],
        frame["gameweek"],
        frame.get("histories", {}),
    )
    pools = {
        str(k): top_projected(predictions[REFERENCE], k)
        | top_projected(predictions[FULL], k)
        for k in top_ks
    }
    models = {}
    for name, values in predictions.items():
        decision = evaluate(frame["players"], values, actual)
        models[name] = {
            "decision": {
                key: decision[key]
                for key in (
                    "selected_xi",
                    "captain",
                    "xi_actual",
                    "captain_actual",
                    "total_actual",
                    "total_predicted",
                )
                if key in decision
            },
            "candidates": {
                k: candidate_scores(values, actual, ids) for k, ids in pools.items()
            },
            "groups": {
                k: {
                    kind: {
                        group: candidate_scores(values, actual, ids & members)
                        for group, members in partition.items()
                    }
                    for kind, partition in groups.items()
                }
                for k, ids in pools.items()
            },
        }
    return {
        "season": frame["season"],
        "gameweek": frame["gameweek"],
        "provenance": frame.get("provenance", "provided-snapshot"),
        "candidate_pools": {k: sorted(ids) for k, ids in pools.items()},
        "history_coverage": {
            name: {k: _coverage(ids, used) for k, ids in pools.items()}
            for name, used in usage.items()
        },
        "models": models,
    }


def _summary(weeks, k):
    names = sorted(weeks[0]["models"])

    def scores(name, kind=None, group=None):
        return [
            (
                w["models"][name]["candidates"][k]
                if kind is None
                else w["models"][name]["groups"][k].get(kind, {}).get(group)
            )
            for w in weeks
        ]

    def comparison(name, reference, kind=None, group=None):
        pairs = [
            {"model": a, "reference": b}
            for a, b in zip(scores(name, kind, group), scores(reference, kind, group))
            if a is not None and b is not None
        ]
        result = summarise_pairs(pairs)
        # Pooled metrics already live in models/groups; avoid duplicating them
        # for every comparison and candidate size in the saved artifact.
        result.pop("model")
        result.pop("reference")
        result["gameweeks"] = len(pairs)
        result["player_gameweeks"] = sum(p["model"]["n"] for p in pairs)
        if kind is None:
            result["decision_differences"] = {
                key: bootstrap_paired(
                    w["models"][name]["decision"][key]
                    - w["models"][reference]["decision"][key]
                    for w in weeks
                    if key in w["models"][name]["decision"]
                    and key in w["models"][reference]["decision"]
                )
                for key in ("xi_actual", "captain_actual", "total_actual")
            }
        return result

    groups = {
        kind: sorted(
            {group for w in weeks for group in w["models"][FULL]["groups"][k][kind]}
        )
        for kind in ("position", "fixture", "history")
    }
    result = {"gameweeks": len(weeks), "models": {}}
    for name in names:
        candidate = aggregate_candidates(scores(name))
        counts = [w["history_coverage"][name][k] for w in weeks]
        n = sum(c["players"] for c in counts)
        result["models"][name] = {
            **candidate,
            "history_coverage": {
                "player_gameweeks": n,
                "fallback_share": sum(c["fallback_players"] for c in counts) / n,
            },
            "mean_total_actual": (
                mean(
                    w["models"][name]["decision"]["total_actual"]
                    for w in weeks
                    if "total_actual" in w["models"][name]["decision"]
                )
                if any("total_actual" in w["models"][name]["decision"] for w in weeks)
                else None
            ),
            "groups": {
                kind: {
                    group: aggregate_candidates(
                        [s for s in scores(name, kind, group) if s is not None]
                    )
                    for group in members
                }
                for kind, members in groups.items()
            },
        }
    result["comparisons_to_heuristic"] = {
        name: comparison(name, REFERENCE) for name in names if name != REFERENCE
    }
    result["comparisons_to_full"] = {
        name: comparison(name, FULL) for name in names if name != FULL
    }
    result["full_vs_heuristic_groups"] = {
        kind: {group: comparison(FULL, REFERENCE, kind, group) for group in members}
        for kind, members in groups.items()
    }
    return result


def clean_sheet_validation(frames):
    """All-pool GK/DEF clean-sheet counts, with unknown labels excluded."""
    grouped, counts = defaultdict(lambda: defaultdict(list)), defaultdict(
        lambda: {"gameweeks": 0, "labelled_players": 0, "missing_labels": 0}
    )
    for frame in frames:
        validate_frame(frame)
        model = ComponentForecast(
            frame["players"],
            frame["fixtures"],
            frame["scoring_gameweek"],
            teams=frame.get("teams", []),
            histories=frame.get("histories", {}),
            season=frame["season"],
        )
        pool = {p["id"] for p in frame["players"] if p["element_type"] in (1, 2)}
        predicted = {
            pid: model.components(pid, frame["gameweek"])["components"]["clean_sheet"]
            / 4
            for pid in pool
        }
        actual = {
            int(pid): number(value, float("nan"))
            for pid, value in frame.get("actual_clean_sheets", {}).items()
        }
        known = {pid for pid in pool & actual.keys() if math.isfinite(actual[pid])}
        season = frame["season"]
        counts[season]["gameweeks"] += 1
        counts[season]["labelled_players"] += len(known)
        counts[season]["missing_labels"] += len(pool - known)
        groups = groups_for_frame(
            frame["players"],
            frame["fixtures"],
            frame["gameweek"],
            frame.get("histories", {}),
            frame.get("history_usage"),
        )
        for kind, partition in groups.items():
            for name, ids in partition.items():
                if chosen := ids & known:
                    grouped[season][kind, name].append(
                        moments(predicted, actual, chosen)
                    )
    return {
        season: {
            **counts[season],
            **{
                kind: {
                    name: calibration(rows)
                    for (group, name), rows in grouped[season].items()
                    if group == kind
                }
                for kind in ("position", "fixture", "history")
            },
        }
        for season in counts
    }


def validate_components(
    frames,
    *,
    evaluation_seasons,
    development_seasons=(),
    top_ks=(30, 40, 50),
    progress=None,
    prediction_fn=None,
):
    if set(evaluation_seasons) & set(development_seasons):
        raise ValueError("Development and evaluation seasons must be disjoint")
    if not top_ks or any(k <= 0 for k in top_ks) or len(set(top_ks)) != len(top_ks):
        raise ValueError("Candidate sizes must be distinct positive integers")
    requested = set(evaluation_seasons) | set(development_seasons)
    frames = [f for f in frames if f["season"] in requested]
    if not evaluation_seasons or any(
        not any(f["season"] == season for f in frames) for season in requested
    ):
        raise ValueError("Every requested season must contain evaluable frames")
    identities = [(f["season"], f["gameweek"]) for f in frames]
    if len(set(identities)) != len(identities):
        raise ValueError("Use one pre-deadline snapshot per season/gameweek")
    weeks = []
    for frame in sorted(frames, key=lambda f: (f["season"], f["gameweek"])):
        weeks.append(_week(frame, top_ks, prediction_fn or validation_predictions))
        if progress:
            progress(frame["season"], frame["gameweek"])
    split_weeks = defaultdict(list)
    for week in weeks:
        split_weeks[
            "evaluation" if week["season"] in evaluation_seasons else "development"
        ].append(week)
    summary = {
        split: {str(k): _summary(rows, str(k)) for k in top_ks}
        for split, rows in split_weeks.items()
    }
    # Keep the artifact auditable without duplicating all per-stratum raw scores.
    for week in weeks:
        for model in week["models"].values():
            model.pop("groups")
            for k, score in model["candidates"].items():
                model["candidates"][k] = {
                    key: score[key]
                    for key in (
                        "n",
                        "rmse",
                        "poisson_deviance",
                        "poisson_n",
                        "negative_actuals_excluded_from_deviance",
                        "predictions_floored_for_deviance",
                    )
                }
                model["candidates"][k]["calibration"] = calibration([score["moments"]])
    return {
        "protocol": "component-validation-v1-2026-11-24",
        "model_fingerprint": model_fingerprint(),
        "promotion_eligible": False,
        "promotion_note": "Historical ablations are development evidence; the registered live policy still applies.",
        "top_k": list(top_ks),
        "summary": summary,
        "weeks": weeks,
    }
