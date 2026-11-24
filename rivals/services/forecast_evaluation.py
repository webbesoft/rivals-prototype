"""Comparable forecast scores and hindsight benchmarks, without external packages."""

import math
from collections import defaultdict
from functools import lru_cache
from statistics import mean

from .xi_benchmark import best_projected_xi
from .forecast_diagnostics import moments, calibration, groups_for_frame, coverage


def ranks(values):
    ordered = sorted(range(len(values)), key=lambda i: values[i])
    result = [0.0] * len(values)
    start = 0
    while start < len(values):
        end = start + 1
        while end < len(values) and values[ordered[end]] == values[ordered[start]]:
            end += 1
        for i in ordered[start:end]:
            result[i] = (start + end - 1) / 2 + 1
        start = end
    return result


def spearman(predicted, actual):
    x, y = ranks(predicted), ranks(actual)
    if len(x) < 2:
        return None
    mx, my = mean(x), mean(y)
    denominator = math.sqrt(
        sum((v - mx) ** 2 for v in x) * sum((v - my) ** 2 for v in y)
    )
    return (
        sum((a - mx) * (b - my) for a, b in zip(x, y)) / denominator
        if denominator
        else None
    )


def select_xi(players, predictions):
    """Maximise XI plus captain bonus under the shared benchmark constraints."""
    return _select_xi(
        tuple(
            sorted(
                (p["id"], p["team"], p["element_type"], predictions[p["id"]])
                for p in players
                if p["id"] in predictions
            )
        )
    )


@lru_cache(maxsize=32)
def _select_xi(rows):
    return best_projected_xi(
        [
            {
                "id": pid,
                "team": club,
                "element_type": position,
                "expected_points": points,
            }
            for pid, club, position, points in rows
        ],
        captaincy=True,
    )


def evaluate(
    players,
    predictions,
    actual,
    *,
    selected=None,
    fixtures=(),
    week=None,
    selection_metrics=True,
):
    pool = [p for p in players if p["id"] in predictions]
    if selected:
        ids = selected["ids"]
        required = set(ids) | {selected["captain"]}
        if (
            selected["captain"] not in ids
            or not required <= set(predictions)
            or not required <= set(actual)
        ):
            raise ValueError(
                "Complete scores and projections are required for the stored XI and captain"
            )
    if not pool or any(p["id"] not in actual for p in pool):
        raise ValueError("Complete scores are required for the forecast player pool")
    errors = [predictions[p["id"]] - actual[p["id"]] for p in pool]
    positions, bands = defaultdict(list), defaultdict(list)
    for p in pool:
        positions[p["element_type"]].append(p["id"])
        bands[math.floor(predictions[p["id"]])].append(p["id"])
    selected = (selected or select_xi(pool, predictions)) if selection_metrics else None
    hindsight = select_xi(pool, actual) if selection_metrics else None
    result = {
        "players": len(pool),
        "mae": mean(abs(e) for e in errors),
        "rmse": math.sqrt(mean(e * e for e in errors)),
        "bias": mean(errors),
        "spearman_by_position": {
            str(pos): spearman([predictions[i] for i in ids], [actual[i] for i in ids])
            for pos, ids in positions.items()
        },
        "calibration_by_position": {
            str(pos): {
                "n": len(ids),
                "predicted": mean(predictions[i] for i in ids),
                "actual": mean(actual[i] for i in ids),
            }
            for pos, ids in positions.items()
        },
        "calibration_by_prediction_band": {
            str(band): {
                "n": len(ids),
                "predicted": mean(predictions[i] for i in ids),
                "actual": mean(actual[i] for i in ids),
            }
            for band, ids in sorted(bands.items())
        },
    }
    if week is not None:
        from .player_forecast import PlayerForecast, number

        schedule = PlayerForecast(pool, fixtures, 0).fixtures
        fixture_groups = defaultdict(list)
        for p in pool:
            matches = schedule.get((p["team"], week), [])
            values = [
                number(f.get("team_h_difficulty" if home else "team_a_difficulty"), 3)
                for f, home in matches
            ]
            band = str(round(mean(values), 1)) if values else "blank"
            fixture_groups[band].append(p["id"])
        result["calibration_by_fixture_band"] = {
            band: {
                "n": len(ids),
                "predicted": mean(predictions[i] for i in ids),
                "actual": mean(actual[i] for i in ids),
            }
            for band, ids in sorted(fixture_groups.items())
        }
    if selected and hindsight:
        ids = selected["ids"]
        captain = selected["captain"]
        xi_actual = sum(actual[i] for i in ids)
        result.update(
            {
                "selected_xi": list(ids),
                "captain": captain,
                "xi_predicted": sum(predictions[i] for i in ids),
                "xi_actual": xi_actual,
                "total_predicted": sum(predictions[i] for i in ids)
                + predictions[captain],
                "captain_actual": actual[captain],
                "total_actual": xi_actual + actual[captain],
                "hindsight_xi": list(hindsight["ids"]),
                "hindsight_captain": hindsight["captain"],
                "hindsight_total": hindsight["points"],
                "regret": hindsight["points"] - xi_actual - actual[captain],
                "xi_overlap": len(set(ids) & set(hindsight["ids"])),
            }
        )
    return result


def candidate_scores(predictions, actual, ids):
    """RMSE includes negative points; Poisson deviance cannot include them."""
    ids = sorted(ids)
    if not ids:
        return None
    nonnegative = [i for i in ids if actual[i] >= 0]
    epsilon = 1e-6
    deviance = []
    buckets = defaultdict(list)
    for pid in ids:
        buckets[math.floor(predictions[pid])].append(pid)
    for pid in nonnegative:
        y, mu = actual[pid], max(epsilon, predictions[pid])
        deviance.append(2 * (y * math.log(y / mu) - y + mu) if y else 2 * mu)
    return {
        "moments": moments(predictions, actual, ids),
        "ids": ids,
        "n": len(ids),
        "rmse": math.sqrt(mean((predictions[i] - actual[i]) ** 2 for i in ids)),
        "poisson_deviance": mean(deviance) if deviance else None,
        "poisson_n": len(nonnegative),
        "negative_actuals_excluded_from_deviance": len(ids) - len(nonnegative),
        "predictions_floored_for_deviance": sum(
            predictions[i] < epsilon for i in nonnegative
        ),
        "calibration": {
            str(b): {
                "n": len(group),
                "predicted": mean(predictions[i] for i in group),
                "actual": mean(actual[i] for i in group),
            }
            for b, group in sorted(buckets.items())
        },
    }


def top_projected(predictions, k):
    return set(sorted(predictions, key=lambda i: (-predictions[i], i))[:k])


def evaluate_models(
    players,
    predictions,
    actual,
    *,
    fixtures=(),
    week=None,
    selections=None,
    top_k=40,
    frame=None,
):
    """Own top-K for relevance; pairwise union for a fair common candidate pool."""
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    metrics = {
        model: evaluate(
            players,
            values,
            actual,
            selected=(selections or {}).get(model),
            fixtures=fixtures,
            week=week,
        )
        for model, values in predictions.items()
    }
    groups = groups_for_frame(
        players,
        fixtures,
        week,
        (frame or {}).get("histories", {}),
        (frame or {}).get("history_usage"),
    )
    for model, values in predictions.items():
        full = metrics[model]
        full["coverage"] = coverage(groups, values)
        full["history_strata"] = {}
        for name, ids in groups["history"].items():
            subset = {i: values[i] for i in set(values) & ids}
            if not subset:
                continue
            group = evaluate(
                players,
                subset,
                actual,
                fixtures=fixtures,
                week=week,
                selection_metrics=False,
            )
            group["top_candidates"] = candidate_scores(
                values, actual, top_projected(values, top_k) & ids
            )
            if "selected_xi" in full:
                starters = set(full["selected_xi"]) & ids
                captain_points = (
                    actual[full["captain"]] if full["captain"] in ids else 0
                )
                group.update(
                    xi_actual=sum(actual[i] for i in starters),
                    captain_actual=captain_points,
                    total_actual=sum(actual[i] for i in starters) + captain_points,
                    selected_starters=len(starters),
                    captain_in_stratum=full["captain"] in ids,
                    selection_note="contributions of frozen full-pool XI; no stratum reoptimisation",
                )
            full["history_strata"][name] = group
    reference = "heuristic-v1"
    baseline = predictions.get(reference, {})
    paired, paired_history = {}, {}
    for model, values in predictions.items():
        metrics[model]["top_candidates"] = candidate_scores(
            values, actual, top_projected(values, top_k)
        )
        if model == reference or not baseline:
            continue
        common = set(values) & set(baseline)
        own_common = {i: values[i] for i in common}
        old_common = {i: baseline[i] for i in common}
        union = top_projected(own_common, top_k) | top_projected(old_common, top_k)
        pair = {
            "model": candidate_scores(values, actual, union),
            "reference": candidate_scores(baseline, actual, union),
        }
        pair["coverage"] = coverage(groups, union)
        pair["groups"] = {
            kind: {
                name: {
                    "model": candidate_scores(values, actual, union & ids),
                    "reference": candidate_scores(baseline, actual, union & ids),
                }
                for name, ids in partition.items()
            }
            for kind, partition in groups.items()
        }
        metrics[model]["paired_candidate_pool"] = pair
        for name, group in metrics[model]["history_strata"].items():
            group["paired_candidate_pool"] = pair["groups"]["history"][name]

        # Reuse frozen decisions when pools match; otherwise compare decisions
        # from the same pre-deadline common pool, including ep_next's coverage.
        if set(values) == set(baseline):
            chosen, old = metrics[model], metrics[reference]
        elif common:
            chosen = evaluate(players, own_common, actual)
            old = evaluate(players, old_common, actual)
        else:
            continue
        if "total_actual" in chosen and "total_actual" in old:
            paired[model] = {
                key: chosen[key] - old[key]
                for key in ("xi_actual", "captain_actual", "total_actual")
            }
            paired_history[model] = {}
            for name, ids in groups["history"].items():
                xi_delta = sum(
                    actual[i] for i in set(chosen["selected_xi"]) & ids
                ) - sum(actual[i] for i in set(old["selected_xi"]) & ids)
                captain_delta = (
                    actual[chosen["captain"]] if chosen["captain"] in ids else 0
                ) - (actual[old["captain"]] if old["captain"] in ids else 0)
                paired_history[model][name] = {
                    "xi_actual": xi_delta,
                    "captain_actual": captain_delta,
                    "total_actual": xi_delta + captain_delta,
                }

    for model_name in (
        name
        for name in (
            "decision-v11",
            "decision-v12-calibrated",
            "decision-v12-fixture-only",
        )
        if frame and name in predictions
    ):
        details = frame.get("challenger_details_by_model", {}).get(model_name)
        if details is None and model_name == "decision-v11":
            details = frame.get("challenger_details")
        if details is None:
            from .component_forecast import ComponentForecast
            from .calibrated_component_forecast import (
                CalibratedComponentForecast,
                identity_parameters,
            )

            kwargs = dict(
                teams=frame.get("teams", []),
                histories={int(k): v for k, v in frame.get("histories", {}).items()},
                season=frame["season"],
            )
            cls = ComponentForecast
            if model_name != "decision-v11":
                cls = CalibratedComponentForecast
                parameters = frame.get("revised_parameters")
                if model_name == "decision-v12-fixture-only":
                    if parameters is None:
                        raise ValueError(
                            "Fixture control requires frozen calibration parameters"
                        )
                    parameters = identity_parameters(parameters["strength_power"])
                kwargs["parameters"] = parameters
            model = cls(players, fixtures, frame["scoring_gameweek"], **kwargs)
        labels = {int(k): v for k, v in frame.get("actual_clean_sheets", {}).items()}
        cs_predictions = {
            p["id"]: (
                (
                    details.get(str(p["id"]), details.get(p["id"]))
                    if details is not None
                    else model.components(p["id"], week)
                )["components"]["clean_sheet"]
                / 4
            )
            for p in players
            if p["element_type"] in (1, 2)
        }
        clean = {
            "labelled_players": len(set(labels) & set(cs_predictions)),
            "missing_labels": len(set(cs_predictions) - set(labels)),
        }
        for kind, partition in groups.items():
            clean[kind] = {
                name: moments(
                    cs_predictions, labels, ids & set(cs_predictions) & set(labels)
                )
                for name, ids in partition.items()
            }
        metrics[model_name]["clean_sheet_calibration"] = clean
        for name, group in metrics[model_name]["history_strata"].items():
            ids = groups["history"][name]
            group["clean_sheet_calibration"] = {
                kind: {
                    key: moments(
                        cs_predictions,
                        labels,
                        members & ids & set(cs_predictions) & set(labels),
                    )
                    for key, members in partition.items()
                }
                for kind, partition in groups.items()
            }
    return {
        "metrics": metrics,
        "paired_differences": paired,
        "paired_history_differences": paired_history,
        "reference": reference,
        "top_k": top_k,
    }


def bootstrap_paired(differences, *, samples=5000, seed=1729):
    """Percentile interval for mean paired GW difference, never player resampling."""
    import random

    values = list(differences)
    if not values:
        return {"gameweeks": 0, "mean": None, "interval_95": None}
    rng = random.Random(seed)
    estimates = sorted(mean(rng.choices(values, k=len(values))) for _ in range(samples))

    def percentile(q):
        index = (len(estimates) - 1) * q
        low = math.floor(index)
        return estimates[low] + (estimates[math.ceil(index)] - estimates[low]) * (
            index - low
        )

    interval = [percentile(0.025), percentile(0.975)] if len(values) > 1 else None
    return {
        "gameweeks": len(values),
        "mean": mean(values),
        "interval_95": interval,
        "includes_zero": interval[0] <= 0 <= interval[1] if interval else None,
        "resampling_unit": "gameweek",
        "samples": samples,
        "seed": seed,
    }


def aggregate_candidates(rows):
    if not rows:
        return None
    n, poisson_n = sum(r["n"] for r in rows), sum(r["poisson_n"] for r in rows)
    buckets = defaultdict(list)
    for row in rows:
        for bucket, values in row["calibration"].items():
            buckets[bucket].append(values)
    return {
        "gameweeks": len(rows),
        "calibration_fit": calibration([r["moments"] for r in rows if "moments" in r]),
        "player_gameweeks": n,
        "rmse": math.sqrt(sum(r["rmse"] ** 2 * r["n"] for r in rows) / n),
        "poisson_deviance": (
            sum((r["poisson_deviance"] or 0) * r["poisson_n"] for r in rows) / poisson_n
            if poisson_n
            else None
        ),
        "poisson_n": poisson_n,
        "negative_actuals_excluded_from_deviance": sum(
            r["negative_actuals_excluded_from_deviance"] for r in rows
        ),
        "predictions_floored_for_deviance": sum(
            r["predictions_floored_for_deviance"] for r in rows
        ),
        "calibration": {
            b: {
                "n": sum(v["n"] for v in group),
                "predicted": sum(v["predicted"] * v["n"] for v in group)
                / sum(v["n"] for v in group),
                "actual": sum(v["actual"] * v["n"] for v in group)
                / sum(v["n"] for v in group),
            }
            for b, group in sorted(buckets.items(), key=lambda item: float(item[0]))
        },
    }


def summarise_pairs(rows):
    paired = [r for r in rows if r.get("model") and r.get("reference")]
    result = {
        key: aggregate_candidates([r[key] for r in paired])
        for key in ("model", "reference")
    }
    result["paired_error_differences"] = {
        metric: bootstrap_paired(
            r["model"][metric] - r["reference"][metric]
            for r in paired
            if r["model"][metric] is not None and r["reference"][metric] is not None
        )
        for metric in ("rmse", "poisson_deviance")
    }
    counts = [r["coverage"] for r in rows if r.get("coverage")]
    if counts:
        n = sum(r["players"] for r in counts)
        result["coverage"] = {
            "player_gameweeks": n,
            "fallback_share": (
                sum(r["fallback_players"] for r in counts) / n if n else None
            ),
        }
    return result


def summarise_experiments(weeks):
    """Decision metrics are paired by GW; calibration pools counts, not means."""
    paired, candidates, common_candidates = {}, {}, {}
    models = sorted({m for w in weeks for m in w["metrics"]})
    for model in models:
        rows = [
            w["metrics"][model]["top_candidates"]
            for w in weeks
            if model in w["metrics"] and w["metrics"][model].get("top_candidates")
        ]
        if rows:
            candidates[model] = aggregate_candidates(rows)
        common = [
            w["metrics"][model]["paired_candidate_pool"]
            for w in weeks
            if model in w["metrics"] and "paired_candidate_pool" in w["metrics"][model]
        ]
        if common:
            common_candidates[model] = summarise_pairs(common)
            common_candidates[model]["groups"] = {
                kind: {
                    name: summarise_pairs(
                        [
                            r["groups"][kind][name]
                            for r in common
                            if name in r.get("groups", {}).get(kind, {})
                        ]
                    )
                    for name in sorted(
                        {
                            name
                            for r in common
                            for name in r.get("groups", {}).get(kind, {})
                        }
                    )
                }
                for kind in ("position", "fixture", "history")
            }

        differences = [
            w["paired_differences"][model]
            for w in weeks
            if model in w.get("paired_differences", {})
        ]
        if differences:
            paired[model] = {
                key: bootstrap_paired(row[key] for row in differences)
                for key in ("xi_actual", "captain_actual", "total_actual")
            }
    strata = {}
    if any("history_strata" in v for w in weeks for v in w["metrics"].values()):
        for name in ("has_history", "fallback"):
            stratified = [
                {
                    "metrics": {
                        m: v["history_strata"][name]
                        for m, v in w["metrics"].items()
                        if name in v.get("history_strata", {})
                    },
                    "paired_differences": {
                        m: groups[name]
                        for m, groups in w.get("paired_history_differences", {}).items()
                        if name in groups
                    },
                }
                for w in weeks
            ]
            strata[name] = summarise_experiments(stratified)
    diagnostics = {}
    for model in models:
        rows = [w["metrics"][model] for w in weeks if model in w["metrics"]]
        diagnostics[model] = {
            "whole_pool_player_gameweeks": sum(r.get("players", 0) for r in rows)
        }
        n = diagnostics[model]["whole_pool_player_gameweeks"]
        if n:
            diagnostics[model].update(
                mae=sum(r["mae"] * r["players"] for r in rows) / n,
                rmse=math.sqrt(sum(r["rmse"] ** 2 * r["players"] for r in rows) / n),
                bias=sum(r["bias"] * r["players"] for r in rows) / n,
            )
        for field in ("xi_actual", "captain_actual", "total_actual"):
            if any(field in r for r in rows):
                diagnostics[model]["mean_" + field] = sum(
                    r.get(field, 0) for r in rows
                ) / len(rows)
        diagnostics[model]["mean_weekly_spearman_by_position"] = {
            pos: mean(
                r["spearman_by_position"][pos]
                for r in rows
                if r.get("spearman_by_position", {}).get(pos) is not None
            )
            for pos in ("1", "2", "3", "4")
            if any(r.get("spearman_by_position", {}).get(pos) is not None for r in rows)
        }
        for key in (
            "calibration_by_position",
            "calibration_by_fixture_band",
            "calibration_by_prediction_band",
        ):
            diagnostics[model][key] = {
                group: {
                    "n": sum(v["n"] for v in values),
                    "predicted": sum(v["predicted"] * v["n"] for v in values)
                    / sum(v["n"] for v in values),
                    "actual": sum(v["actual"] * v["n"] for v in values)
                    / sum(v["n"] for v in values),
                }
                for group in {group for r in rows for group in r.get(key, {})}
                if (values := [r[key][group] for r in rows if group in r.get(key, {})])
            }
        exposures = [r["coverage"] for r in rows if "coverage" in r]
        if exposures:
            count = sum(r["players"] for r in exposures)
            diagnostics[model]["fallback_share"] = (
                sum(r["fallback_players"] for r in exposures) / count if count else None
            )
    clean_sheet = {}
    for model in models:
        cs = [
            w["metrics"][model]["clean_sheet_calibration"]
            for w in weeks
            if model in w["metrics"]
            and "clean_sheet_calibration" in w["metrics"][model]
        ]
        if cs:
            clean_sheet[model] = {
                kind: {
                    name: calibration(
                        [r[kind][name] for r in cs if name in r.get(kind, {})]
                    )
                    for name in sorted({name for r in cs for name in r.get(kind, {})})
                }
                for kind in ("position", "fixture", "history")
            }
    return {
        "diagnostics": diagnostics,
        "history_strata": strata,
        "clean_sheet_calibration": clean_sheet,
        "top_candidates": candidates,
        "paired_candidate_pools": common_candidates,
        "paired_vs_heuristic": paired,
        "interval_note": "95% percentile bootstrap of paired gameweeks; temporal dependence is not modelled. Zero-spanning intervals are inconclusive, not proof of equivalence.",
    }
