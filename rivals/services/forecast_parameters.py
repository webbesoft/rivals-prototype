"""Frozen hypotheses for v11; changes require a new model experiment."""

import hashlib
import json
from pathlib import Path

COMPONENT_CONFIG = {
    "recency_weight": 0.75,
    "horizon_decay": 0.85,
    "season_prior_minutes": 900.0,
    "recent_prior_minutes": 450.0,
    "double_rotation": 0.90,
}


def model_fingerprint():
    root = Path(__file__).parent
    digest = hashlib.sha256(json.dumps(COMPONENT_CONFIG, sort_keys=True).encode())
    for name in (
        "component_forecast.py",
        "player_forecast.py",
        "historical_forecast_features.py",
    ):
        digest.update((root / name).read_bytes())
    return digest.hexdigest()
