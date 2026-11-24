"""Versioned shadow revision; legacy v11 and its fingerprint remain untouched."""

import hashlib
import json
import math
from copy import deepcopy
from functools import lru_cache
from pathlib import Path

from .component_forecast import ComponentForecast
from .forecast_parameters import model_fingerprint

MODEL_VERSION = "decision-v12-calibrated"
PARAMETER_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "component-calibration-parameters.json"
)


def identity_parameters(strength_power=1.0):
    return {
        "version": MODEL_VERSION,
        "strength_power": strength_power,
        "calibration": {str(p): {"slope": 1.0, "offset": 0.0} for p in (3, 4)},
    }


@lru_cache(maxsize=1)
def load_parameters():
    parameters = json.loads(PARAMETER_PATH.read_text())
    if parameters.get("version") != MODEL_VERSION:
        raise ValueError("Unexpected calibrated component artifact version")
    return parameters


def shadow_fingerprint(parameters=None):
    parameters = parameters if parameters is not None else load_parameters()
    digest = hashlib.sha256(model_fingerprint().encode())
    digest.update(Path(__file__).read_bytes())
    digest.update(json.dumps(parameters, sort_keys=True, allow_nan=False).encode())
    return digest.hexdigest()


class CalibratedComponentForecast(ComponentForecast):
    version = MODEL_VERSION

    def __init__(self, *args, parameters=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.parameters = deepcopy(
            parameters if parameters is not None else load_parameters()
        )
        if self.parameters.get("version") != MODEL_VERSION:
            raise ValueError("Unexpected calibrated component artifact version")
        if not 0 <= self.parameters["strength_power"] <= 1:
            raise ValueError("Fixture strength power must be between zero and one")
        for position in ("3", "4"):
            coefficients = self.parameters["calibration"][position]
            if (
                not all(math.isfinite(coefficients[k]) for k in ("slope", "offset"))
                or coefficients["slope"] <= 0
            ):
                raise ValueError(
                    "Attacker calibration must be finite with a positive slope"
                )

    def strength(self, team, field):
        return super().strength(team, field) ** self.parameters["strength_power"]

    def components(self, pid, week):
        result = super().components(pid, week)
        raw = result["points"]
        appearances = result["minutes"]["play"] * len(
            self.fixtures.get((self.players[pid]["team"], week), [])
        )
        coefficients = self.parameters["calibration"].get(
            str(self.players[pid]["element_type"]), {"slope": 1.0, "offset": 0.0}
        )
        points = max(
            0.0, coefficients["slope"] * raw + coefficients["offset"] * appearances
        )
        result["components"]["calibration"] = points - raw
        result.update(
            points=points,
            uncalibrated_points=raw,
            expected_appearance_count=appearances,
        )
        return result
