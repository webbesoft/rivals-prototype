"""Small per-position ridge challenger fitted only on explicitly training seasons."""

import math
from collections import defaultdict
from statistics import mean

from .component_forecast import ComponentForecast
from .forecast_parameters import COMPONENT_CONFIG, model_fingerprint

FEATURES = (
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
)


def component_model(frame):
    return ComponentForecast(
        frame["players"],
        frame["fixtures"],
        frame["scoring_gameweek"],
        teams=frame.get("teams", []),
        histories={int(k): v for k, v in frame.get("histories", {}).items()},
        season=frame["season"],
    )


def features(model, pid, week):
    parts = model.components(pid, week)["components"]
    return [1.0] + [parts[key] for key in FEATURES]


def solve(matrix, vector):
    rows = [list(row) + [value] for row, value in zip(matrix, vector)]
    n = len(rows)
    for column in range(n):
        pivot = max(range(column, n), key=lambda i: abs(rows[i][column]))
        rows[column], rows[pivot] = rows[pivot], rows[column]
        divisor = rows[column][column]
        if abs(divisor) < 1e-12:
            raise ValueError("Insufficient data to fit ridge model")
        rows[column] = [value / divisor for value in rows[column]]
        for i in range(n):
            if i != column:
                multiplier = rows[i][column]
                rows[i] = [a - multiplier * b for a, b in zip(rows[i], rows[column])]
    return [row[-1] for row in rows]


def fit_ridge(frames, penalty=100.0):
    """Fit residual corrections on training-standardised features, per position."""
    if penalty <= 0:
        raise ValueError("Ridge penalty must be positive")
    frames = list(frames)
    size = len(FEATURES) + 1
    observations = defaultdict(list)
    for frame in frames:
        model = component_model(frame)
        actual = {int(k): float(v) for k, v in frame["actual"].items()}
        for player in frame["players"]:
            pid, pos = player["id"], player["element_type"]
            if pid in actual:
                x = features(model, pid, frame["gameweek"])
                observations[pos].append((x, actual[pid] - sum(x[1:])))
    coefficients, standardization, corrections = {}, {}, {}
    for pos in (1, 2, 3, 4):
        rows = observations[pos]
        if len(rows) < size:
            raise ValueError(f"Too few training samples for position {pos}")
        means = [mean(x[i] for x, _ in rows) for i in range(1, size)]
        scales = [
            math.sqrt(mean((x[i + 1] - means[i]) ** 2 for x, _ in rows)) or 1.0
            for i in range(size - 1)
        ]
        matrix, vector = [[0.0] * size for _ in range(size)], [0.0] * size
        for x, residual in rows:
            z = [1.0] + [(v - m) / scale for v, m, scale in zip(x[1:], means, scales)]
            for i in range(size):
                vector[i] += z[i] * residual
                for j in range(size):
                    matrix[i][j] += z[i] * z[j]
        # The intercept is unpenalised; all other corrections have unit-scale inputs.
        for i in range(1, size):
            matrix[i][i] += penalty
        correction = solve(matrix, vector)
        standardization[str(pos)] = {"means": means, "scales": scales}
        corrections[str(pos)] = correction
        raw_correction = [value / scale for value, scale in zip(correction[1:], scales)]
        coefficients[str(pos)] = [
            correction[0] - sum(c * m for c, m in zip(raw_correction, means))
        ] + [1 + c for c in raw_correction]
    return {
        "version": "decision-v11-ridge-standardized-v1",
        "features": ["intercept", *FEATURES],
        "penalty": penalty,
        "intercept_penalized": False,
        "standardization": standardization,
        "standardized_corrections": corrections,
        "coefficients": coefficients,
        "training_samples": {pos: len(rows) for pos, rows in observations.items()},
        "training_seasons": sorted({f["season"] for f in frames}),
        "training_provenance": sorted(
            {f.get("provenance", "provided-snapshot") for f in frames}
        ),
        "component_config": dict(COMPONENT_CONFIG),
        "component_model_fingerprint": model_fingerprint(),
        "live_enabled": False,
    }


def predict_ridge(frame, artifact):
    if "standardization" not in artifact:
        raise ValueError("Unstandardised ridge artifacts must be refitted")
    model = component_model(frame)
    result = {}
    for p in frame["players"]:
        if not model.fixtures.get((p["team"], frame["gameweek"])):
            result[p["id"]] = 0.0
            continue
        pos = str(p["element_type"])
        x = features(model, p["id"], frame["gameweek"])
        transform = artifact["standardization"][pos]
        z = [1.0] + [
            (v - m) / scale
            for v, m, scale in zip(x[1:], transform["means"], transform["scales"])
        ]
        result[p["id"]] = sum(x[1:]) + sum(
            a * b for a, b in zip(z, artifact["standardized_corrections"][pos])
        )
    return result
