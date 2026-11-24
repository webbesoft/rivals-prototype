"""Earlier-season fitting and chronological selection for the v12 shadow model."""

import math
from collections import defaultdict
from statistics import mean

from .calibrated_component_forecast import (
    CalibratedComponentForecast,
    identity_parameters,
)
from .component_forecast import ComponentForecast
from .forecast_evaluation import candidate_scores, top_projected
from .forecast_ridge import solve
from .player_forecast import PlayerForecast


def model(frame, parameters):
    return CalibratedComponentForecast(
        frame["players"],
        frame["fixtures"],
        frame["scoring_gameweek"],
        teams=frame.get("teams", []),
        histories=frame.get("histories", {}),
        season=frame["season"],
        parameters=parameters,
    )


def candidate_pool(frame):
    old = PlayerForecast(frame["players"], frame["fixtures"], frame["scoring_gameweek"])
    full = ComponentForecast(
        frame["players"],
        frame["fixtures"],
        frame["scoring_gameweek"],
        teams=frame.get("teams", []),
        histories=frame.get("histories", {}),
        season=frame["season"],
    )
    week = frame["gameweek"]
    return top_projected(
        {p["id"]: old.points(p["id"], week) for p in frame["players"]}, 40
    ) | top_projected(
        {p["id"]: full.points(p["id"], week) for p in frame["players"]}, 40
    )


def fit_parameters(frames, *, strength_power, penalty=100.0):
    from .forecast_backtest import validate_frame

    if penalty <= 0:
        raise ValueError("Calibration penalty must be positive")
    frames = list(frames)
    observations = defaultdict(list)
    for frame in frames:
        validate_frame(frame)
        raw_model = model(frame, identity_parameters(strength_power))
        actual = {int(k): float(v) for k, v in frame["actual"].items()}
        candidates = candidate_pool_cached(frame)
        for p in frame["players"]:
            if p["element_type"] not in (3, 4) or p["id"] not in candidates:
                continue
            details = raw_model.components(p["id"], frame["gameweek"])
            x = [details["uncalibrated_points"], details["expected_appearance_count"]]
            y = actual[p["id"]]
            if not all(math.isfinite(v) for v in [*x, y]):
                raise ValueError("Finite calibration features and labels required")
            if x[1] > 0:
                observations[p["element_type"]].append((x, y - x[0]))
    artifact = identity_parameters(strength_power)
    for position in (3, 4):
        rows = observations[position]
        if len(rows) < 30:
            raise ValueError(f"Too few candidate observations for position {position}")
        scales = [math.sqrt(mean(x[i] ** 2 for x, _ in rows)) or 1.0 for i in range(2)]
        matrix = [[0.0] * 2 for _ in range(2)]
        vector = [0.0] * 2
        for x, residual in rows:
            z = [v / scale for v, scale in zip(x, scales)]
            for i in range(2):
                vector[i] += z[i] * residual
                for j in range(2):
                    matrix[i][j] += z[i] * z[j]
        for i in range(2):
            matrix[i][i] += penalty
        corrections = solve(matrix, vector)
        artifact["calibration"][str(position)] = {
            "slope": 1 + corrections[0] / scales[0],
            "offset": corrections[1] / scales[1],
        }
        if artifact["calibration"][str(position)]["slope"] <= 0:
            raise ValueError("Fitted attacker slope must stay positive")
    artifact.update(
        penalty=penalty,
        training_samples={str(k): len(v) for k, v in observations.items()},
        training_seasons=sorted({f["season"] for f in frames}),
        fitting="RMS-scaled residual ridge toward identity; no free points for non-appearances",
    )
    return artifact


def candidate_pool_cached(frame):
    # A caller-owned derived key avoids global frame caches or retaining labels.
    # The training/selection command supplies this after computing it from inputs.
    return frame.get("calibration_candidate_pool") or candidate_pool(frame)


def select_parameters(
    frames,
    *,
    fit_seasons,
    selection_seasons,
    powers=(0, 0.25, 0.5, 0.75, 1),
    penalty=100.0,
    progress=None,
):
    from .forecast_backtest import validate_frame

    if (
        set(fit_seasons) & set(selection_seasons)
        or not fit_seasons
        or not selection_seasons
    ):
        raise ValueError("Fit and selection seasons must be nonempty and disjoint")
    if max(fit_seasons) >= min(selection_seasons):
        raise ValueError("Fit seasons must precede selection seasons")
    requested = set(fit_seasons) | set(selection_seasons)
    frames = [{**f} for f in frames if f["season"] in requested]
    if any(not any(f["season"] == s for f in frames) for s in requested):
        raise ValueError("Every requested season must contain evaluable frames")
    identities = [(f["season"], f["gameweek"]) for f in frames]
    if len(identities) != len(set(identities)):
        raise ValueError("Duplicate calibration gameweeks")
    for frame in frames:
        validate_frame(frame)
        frame["calibration_candidate_pool"] = candidate_pool(frame)
    training = [f for f in frames if f["season"] in fit_seasons]
    selection = [f for f in frames if f["season"] in selection_seasons]
    scores = []
    for power in powers:
        parameters = fit_parameters(training, strength_power=power, penalty=penalty)
        weekly = []
        for frame in selection:
            forecast = model(frame, parameters)
            values = {
                p["id"]: forecast.points(p["id"], frame["gameweek"])
                for p in frame["players"]
            }
            actual = {int(k): float(v) for k, v in frame["actual"].items()}
            metrics = candidate_scores(values, actual, candidate_pool_cached(frame))
            weekly.append(
                {
                    "season": frame["season"],
                    "gameweek": frame["gameweek"],
                    "rmse": metrics["rmse"],
                    "poisson_deviance": metrics["poisson_deviance"],
                    "n": metrics["n"],
                }
            )
        scores.append(
            {
                "strength_power": power,
                "mean_weekly_rmse": mean(w["rmse"] for w in weekly),
                "mean_weekly_deviance": mean(w["poisson_deviance"] for w in weekly),
                "training_parameters": parameters,
                "weeks": weekly,
            }
        )
        if progress:
            progress(power)
    winner = min(
        scores,
        key=lambda s: (
            s["mean_weekly_rmse"],
            s["mean_weekly_deviance"],
            abs(s["strength_power"] - 1),
        ),
    )
    final = fit_parameters(
        frames, strength_power=winner["strength_power"], penalty=penalty
    )
    return final, {
        "fit_seasons": list(fit_seasons),
        "selection_seasons": list(selection_seasons),
        "scores": scores,
        "selected_strength_power": winner["strength_power"],
        "selection_note": "Selection uses earlier seasons only; final artifact is refitted on fit plus selection seasons.",
    }


def revision_predictions(frame, parameters):
    revised = model(frame, parameters)
    fixture_only = model(frame, identity_parameters(parameters["strength_power"]))
    original = ComponentForecast(
        frame["players"],
        frame["fixtures"],
        frame["scoring_gameweek"],
        teams=frame.get("teams", []),
        histories=frame.get("histories", {}),
        season=frame["season"],
    )
    models = {
        "heuristic-v1": PlayerForecast(
            frame["players"], frame["fixtures"], frame["scoring_gameweek"]
        ),
        "decision-v11": original,
        revised.version: revised,
        "v12-fixture-only": fixture_only,
    }
    predictions = {
        name: {p["id"]: m.points(p["id"], frame["gameweek"]) for p in frame["players"]}
        for name, m in models.items()
    }
    used = {pid for pid, rows in original.histories.items() if rows}
    return predictions, {name: used for name in models}
