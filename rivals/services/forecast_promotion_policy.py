"""Read the registered policy; reporting does not activate a challenger."""

import hashlib
import json
from pathlib import Path


def promotion_policy(experiment_version=None):
    filename = (
        "forecast-promotion-v2.json"
        if experiment_version == "forecast-experiment-v4"
        else "forecast-promotion-policy.json"
    )
    path = Path(__file__).resolve().parents[2] / "docs" / filename
    raw = path.read_bytes()
    return {**json.loads(raw), "sha256": hashlib.sha256(raw).hexdigest()}
