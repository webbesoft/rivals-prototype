"""Read the registered policy; reporting does not activate a challenger."""

import hashlib
import json
from pathlib import Path


def promotion_policy():
    path = (
        Path(__file__).resolve().parents[2] / "docs" / "forecast-promotion-policy.json"
    )
    raw = path.read_bytes()
    return {**json.loads(raw), "sha256": hashlib.sha256(raw).hexdigest()}
