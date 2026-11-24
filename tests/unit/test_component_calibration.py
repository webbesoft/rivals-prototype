from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from rivals.services.calibrated_component_forecast import (
    CalibratedComponentForecast,
    identity_parameters,
)
from rivals.services.component_calibration import (
    fit_parameters,
    model,
    select_parameters,
)
from rivals.services.component_forecast import ComponentForecast
from tests.unit.test_component_validation import historical_frame


def test_identity_reproduces_v11_and_strength_damping_changes_only_strength_effect():
    f = historical_frame()
    old = ComponentForecast(
        f["players"],
        f["fixtures"],
        f["scoring_gameweek"],
        teams=f["teams"],
        histories=f["histories"],
    )
    full = model(f, identity_parameters())
    neutral = model(f, identity_parameters(0))
    for p in f["players"]:
        assert full.points(p["id"], 11) == old.points(p["id"], 11)
    details = full.components(13, 11)["components"]
    assert (
        neutral.components(13, 11)["components"]["appearance"] == details["appearance"]
    )
    assert neutral.components(13, 11)["components"]["goals"] != details["goals"]


def test_calibration_respects_absences_availability_blanks_and_component_sum():
    f = historical_frame()
    parameters = identity_parameters(0.25)
    parameters["calibration"]["4"] = {"slope": 0.7, "offset": 2.0}
    full = model(f, parameters)
    before = full.points(13, 11)
    for p in f["players"]:
        p["chance_of_playing_next_round"] = 50
    half = model(f, parameters)
    assert half.points(13, 11) == pytest.approx(before / 2)
    assert half.points(13, 12) == 0
    for p in f["players"]:
        p["status"] = "i"
    absent = model(f, parameters)
    assert absent.points(13, 11) == 0
    d = half.components(13, 11)
    assert sum(d["components"].values()) == pytest.approx(d["points"])


def training_frames(season, n=12):
    result = []
    for offset in range(n):
        f = historical_frame()
        captured = datetime(int(season[:4]), 11, 1, tzinfo=timezone.utc) + timedelta(
            days=offset * 7
        )
        f.update(
            season=season,
            gameweek=11 + offset,
            scoring_gameweek=10 + offset,
            captured_at=captured.isoformat(),
            deadline=(captured + timedelta(days=1)).isoformat(),
            histories={},
        )
        for fixture in f["fixtures"]:
            fixture["event"] = f["gameweek"]
        for p in f["players"]:
            p["expected_goals"] = p["id"] / 5
        raw = model(f, identity_parameters(0.5))
        f["actual"] = {
            p["id"]: 0.5 * raw.points(p["id"], f["gameweek"])
            + 0.3 * raw.components(p["id"], f["gameweek"])["expected_appearance_count"]
            for p in f["players"]
        }
        result.append(f)
    return result


def test_fitting_reduces_attacker_training_error_and_never_consumes_later_labels():
    training = training_frames("2022-23")
    parameters = fit_parameters(training, strength_power=0.5)
    before, after = [], []
    for f in training:
        raw, fitted = model(f, identity_parameters(0.5)), model(f, parameters)
        for p in f["players"]:
            if p["element_type"] in (3, 4):
                y = f["actual"][p["id"]]
                before.append((raw.points(p["id"], f["gameweek"]) - y) ** 2)
                after.append((fitted.points(p["id"], f["gameweek"]) - y) ** 2)
    assert sum(after) < sum(before)
    selection = training_frames("2023-24")
    final, audit = select_parameters(
        training + selection,
        fit_seasons=["2022-23"],
        selection_seasons=["2023-24"],
        powers=[0.5],
    )
    future = deepcopy(selection)
    for f in future:
        f["season"] = "2025-26"
        f["actual"] = dict.fromkeys(f["actual"], 99999)
    changed, changed_audit = select_parameters(
        training + selection + future,
        fit_seasons=["2022-23"],
        selection_seasons=["2023-24"],
        powers=[0.5],
    )
    assert changed == final and changed_audit == audit
    assert final["training_seasons"] == ["2022-23", "2023-24"]


def test_chronology_and_invalid_calibration_are_rejected():
    with pytest.raises(ValueError, match="precede"):
        select_parameters([], fit_seasons=["2024-25"], selection_seasons=["2023-24"])
    p = identity_parameters()
    p["calibration"]["3"]["slope"] = float("nan")
    with pytest.raises(ValueError, match="finite"):
        model(historical_frame(), p)
