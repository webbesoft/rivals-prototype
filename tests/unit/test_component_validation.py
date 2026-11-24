from copy import deepcopy

import pytest

from rivals.services.component_validation import (
    clean_sheet_validation,
    validation_predictions,
    validate_components,
)
from rivals.services.gameweek_forecast_service import forecasts
from tests.unit.test_forecast_evaluation import frame


def historical_frame():
    f = frame()
    f["teams"] = [
        {
            "id": club,
            "strength_attack_home": 1000 + club * 50,
            "strength_attack_away": 1000,
            "strength_defence_home": 1000,
            "strength_defence_away": 1000 + club * 50,
        }
        for club in (1, 2, 3, 4, 5, 100)
    ]
    f["histories"] = {
        p["id"]: [
            {
                "minutes": 90,
                "starts": 1,
                "kickoff_time": "2026-11-01T12:00:00Z",
                "expected_goals": 0.7,
                "expected_assists": 0.3,
            }
        ]
        for p in f["players"]
    }
    return f


def test_full_models_are_unchanged_and_ablation_features_stay_isolated():
    f = historical_frame()
    original = deepcopy(f)
    predictions, usage = validation_predictions(f)
    standard = forecasts(f)
    assert predictions["heuristic-v1"] == standard["heuristic-v1"]
    assert predictions["decision-v11"] == standard["decision-v11"]
    assert f == original
    assert predictions["without-team-strengths"] != predictions["decision-v11"]
    assert predictions["without-xg-xa"] != predictions["decision-v11"]
    assert usage["all-history-fallback"] == set()
    assert 0 < len(usage["half-history-fallback"]) < len(f["players"])
    assert validation_predictions(f) == (predictions, usage)


def test_ablation_cohort_is_fixed_without_looking_at_labels_or_variant_rankings():
    f = historical_frame()
    report = validate_components([f], evaluation_seasons=[f["season"]], top_ks=[3])
    week = report["weeks"][0]
    ids = week["candidate_pools"]["3"]
    assert set(week["models"]) == set(validation_predictions(f)[0])
    for model in week["models"].values():
        assert model["candidates"]["3"]["n"] == len(ids)
    f["actual"] = dict.fromkeys(f["actual"], 999)
    changed = validate_components([f], evaluation_seasons=[f["season"]], top_ks=[3])
    assert changed["weeks"][0]["candidate_pools"] == week["candidate_pools"]
    assert changed["weeks"][0]["history_coverage"] == week["history_coverage"]


def test_validation_rejects_incomplete_labels_duplicates_and_mixed_splits():
    f = historical_frame()
    del f["actual"][1]
    with pytest.raises(ValueError, match="Complete"):
        validate_components([f], evaluation_seasons=[f["season"]])
    f = historical_frame()
    with pytest.raises(ValueError, match="one pre-deadline"):
        validate_components([f, f], evaluation_seasons=[f["season"]])
    with pytest.raises(ValueError, match="disjoint"):
        validate_components(
            [f], evaluation_seasons=[f["season"]], development_seasons=[f["season"]]
        )
    with pytest.raises(ValueError, match="Every requested season"):
        validate_components([f], evaluation_seasons=["2025-26"])


def test_single_week_has_no_confidence_interval_and_never_grants_live_promotion():
    f = historical_frame()
    report = validate_components([f], evaluation_seasons=[f["season"]], top_ks=[3])
    summary = report["summary"]["evaluation"]["3"]
    difference = summary["comparisons_to_heuristic"]["decision-v11"][
        "paired_error_differences"
    ]["rmse"]
    assert difference["gameweeks"] == 1
    assert difference["interval_95"] is None
    assert report["promotion_eligible"] is False
    assert (
        summary["models"]["all-history-fallback"]["history_coverage"]["fallback_share"]
        == 1
    )


def test_feature_isolation_does_not_reintroduce_removed_xg_or_recent_minutes():
    f = historical_frame()
    before, _ = validation_predictions(f)
    for p in f["players"]:
        p["expected_goals"] = 999
        p["expected_assists"] = 999
    for history in f["histories"].values():
        history[0]["expected_goals"] = 999
        history[0]["expected_assists"] = 999
    after, _ = validation_predictions(f)
    assert before["without-xg-xa"] == after["without-xg-xa"]
    assert before["decision-v11"] != after["decision-v11"]
    f = historical_frame()
    for p in f["players"]:
        p["minutes"] = 90  # Recently starting despite low season usage.
    predictions, _ = validation_predictions(f)
    assert predictions["season-minutes"] != predictions["decision-v11"]


def test_clean_sheet_validation_excludes_unknown_labels_and_keeps_position_counts():
    f = historical_frame()
    f["actual_clean_sheets"] = {1: 0, 2: None, 3: 1, 8: 1}
    report = clean_sheet_validation([f])[f["season"]]
    assert report["labelled_players"] == 2  # Midfielder 8 is outside GK/DEF pool.
    assert report["missing_labels"] == 5
    assert report["position"]["1"]["n"] == 1
    assert report["position"]["2"]["n"] == 1
    assert report["position"]["2"]["sum_actual"] == 1
    assert report["position"]["1"]["slope"] is None
    f.pop("actual_clean_sheets")
    unknown = clean_sheet_validation([f])[f["season"]]
    assert unknown["labelled_players"] == 0
    assert unknown["missing_labels"] == 7
    assert unknown["position"] == {}
