import json
from copy import deepcopy
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError

from rivals.models import FplSnapshot, GameweekForecast
from rivals.services.forecast_evaluation import evaluate_models
from rivals.services.forecast_promotion_policy import promotion_policy
from rivals.services.gameweek_forecast_service import (
    forecasts,
    prepare_forecast,
    publish_forecast,
)
from tests.unit.test_forecast_evaluation import NOW, bootstrap, frame


@pytest.mark.django_db
def test_new_capture_preserves_v3_and_legacy_predictions_and_replays_frozen_parameters():
    f = frame()
    old = GameweekForecast.objects.create(
        season=f["season"],
        gameweek=f["gameweek"],
        version="forecast-experiment-v3",
        captured_at=NOW - timedelta(minutes=1),
        deadline=NOW + timedelta(days=3),
        inputs={"sentinel": True},
        predictions={"sentinel": {"1": 7}},
        selections={},
    )
    with patch("django.utils.timezone.now", return_value=NOW):
        prepared = prepare_forecast(bootstrap(f), f["fixtures"])
        assert publish_forecast(prepared)
    old.refresh_from_db()
    assert old.inputs == {"sentinel": True} and old.predictions == {
        "sentinel": {"1": 7}
    }
    assert GameweekForecast.objects.count() == 2
    revised = GameweekForecast.objects.get(version="forecast-experiment-v4")
    baseline = forecasts(f)
    for name in ("heuristic-v1", "decision-v11"):
        assert {int(k): v for k, v in revised.predictions[name].items()} == baseline[
            name
        ]
    frozen = json.loads(json.dumps(prepared["frame"]))
    with patch(
        "rivals.services.calibrated_component_forecast.load_parameters",
        side_effect=AssertionError("must use frozen artifact"),
    ):
        replay = forecasts(frozen)
    assert replay == prepared["predictions"]
    assert "decision-v12-fixture-only" in replay
    assert revised.inputs["promotion_policy"]["challenger"] == "decision-v12-calibrated"


def test_revised_clean_sheet_metrics_use_the_frozen_model_breakdown():
    f = frame()
    prepared = prepare_forecast(bootstrap(f), f["fixtures"], captured_at=NOW)
    frozen = prepared["frame"]
    frozen["actual_clean_sheets"] = {
        p["id"]: 0 for p in f["players"] if p["element_type"] in (1, 2)
    }
    original = deepcopy(
        frozen["challenger_details_by_model"]["decision-v12-calibrated"]
    )
    original[1]["components"]["clean_sheet"] = 4.0
    frozen["challenger_details_by_model"]["decision-v12-calibrated"] = original
    with patch(
        "rivals.services.calibrated_component_forecast.CalibratedComponentForecast.__init__",
        side_effect=AssertionError("frozen details must be used"),
    ):
        scored = evaluate_models(
            f["players"],
            prepared["predictions"],
            f["actual"],
            frame=frozen,
            fixtures=f["fixtures"],
            week=11,
        )
    cs = scored["metrics"]["decision-v12-calibrated"]["clean_sheet_calibration"]
    assert cs["position"]["1"]["sum_predicted"] >= 1


def test_new_policy_keeps_existing_promotion_thresholds():
    old, new = promotion_policy("forecast-experiment-v3"), promotion_policy(
        "forecast-experiment-v4"
    )
    changed = {"id", "registered_on", "experiment_version", "challenger", "sha256"}
    assert {k: v for k, v in old.items() if k not in changed} == {
        k: v for k, v in new.items() if k not in changed
    }


@pytest.mark.django_db
def test_history_check_is_read_only_by_default_and_can_capture_without_network():
    cache.clear()
    f = frame()
    with patch("django.utils.timezone.now", return_value=NOW):
        for key, payload in [("bootstrap", bootstrap(f)), ("fixtures", f["fixtures"])]:
            FplSnapshot.objects.create(
                key=key,
                payload=payload,
                updated_at=NOW,
                expires_at=NOW + timedelta(hours=1),
            )
        output = StringIO()
        call_command("check_forecast_history", stdout=output)
        assert GameweekForecast.objects.count() == 0
        assert json.loads(output.getvalue())["candidate_players"] == 0
        with patch(
            "rivals.services.fpl_api_service.FplApiService.make_request",
            side_effect=AssertionError("no network needed"),
        ):
            output = StringIO()
            call_command("check_forecast_history", capture=True, stdout=output)
    assert GameweekForecast.objects.get().version == "forecast-experiment-v4"
    assert json.loads(output.getvalue())["forecast_captured"] is True
    cache.clear()


@pytest.mark.django_db
def test_history_check_with_missing_sources_never_enqueues_or_writes():
    from rivals.models import SyncState

    with patch(
        "rivals.services.fpl_snapshot_service.request_snapshot",
        side_effect=AssertionError("read-only check must not enqueue"),
    ):
        with pytest.raises(CommandError, match="Fresh published"):
            call_command("check_forecast_history", stdout=StringIO())
    assert SyncState.objects.count() == 0
    assert GameweekForecast.objects.count() == 0
