from datetime import timedelta
from unittest.mock import Mock, patch

import pytest

from rivals.models import FplSnapshot, GameweekForecast, SyncState
from rivals.services.forecast_history_service import (
    read_histories,
    refresh_histories,
    history_readiness,
    priority_candidates,
)
from tests.unit.test_forecast_evaluation import NOW, bootstrap, frame


def history_api():
    api = Mock()
    api.BASE_URL = "https://example.test/api"
    api.fetch_bootstrap_static.return_value = {
        **bootstrap(frame()),
        "elements": [{"id": i} for i in range(1, 81)],
    }
    api.make_request.return_value = {
        "history": [{"minutes": 90, "kickoff_time": "2026-11-01T12:00:00Z"}]
    }
    return api


def saved_candidates():
    return GameweekForecast.objects.create(
        season="2026/27",
        gameweek=11,
        version="forecast-experiment-v3",
        captured_at=NOW - timedelta(minutes=1),
        deadline=NOW + timedelta(days=3),
        inputs={},
        selections={},
        predictions={
            "heuristic-v1": {str(i): float(i) for i in range(1, 81)},
            "decision-v11": {str(i): float(i) for i in range(1, 81)},
        },
    )


@pytest.mark.django_db
def test_history_warmup_fetches_current_candidates_without_starving_other_players():
    saved_candidates()
    api = history_api()
    with patch("django.utils.timezone.now", return_value=NOW):
        result = refresh_histories(api, {"season": "2026/27", "decision_gameweek": 11})
        requested = {int(key.rsplit(":", 1)[1]) for key in result}
        assert len(requested) == api.make_request.call_count == 20
        assert len(requested & set(range(41, 81))) >= 15
        assert len(requested & set(range(1, 41))) >= 5
        for key, payload in result.items():
            FplSnapshot.objects.create(
                key=key,
                payload=payload,
                updated_at=NOW,
                expires_at=NOW + timedelta(hours=6),
            )
        assert len(set(read_histories("2026/27")) & set(range(41, 81))) >= 15


@pytest.mark.django_db
def test_retry_backoff_still_excludes_candidate_before_bounded_selection():
    saved_candidates()
    SyncState.objects.create(
        key="element-retry:2026/27:41",
        kind="forecast_history",
        failures=1,
        next_attempt_at=NOW + timedelta(minutes=5),
    )
    api = history_api()
    with patch("django.utils.timezone.now", return_value=NOW):
        result = refresh_histories(api, {"season": "2026/27", "decision_gameweek": 11})
    assert "element:2026/27:41" not in result
    assert len(result) == 20


@pytest.mark.django_db
def test_readiness_uses_primary_comparison_not_a_diluted_union_of_controls():
    record = saved_candidates()
    record.version = "forecast-experiment-v4"
    record.inputs = {
        "promotion_policy": {
            "challenger": "decision-v12-calibrated",
            "maximum_common_candidate_fallback_share": 0.1,
        }
    }
    record.predictions["decision-v12-calibrated"] = {
        str(i): float(i) for i in range(1, 81)
    }
    record.predictions["decision-v11"] = {str(i): float(81 - i) for i in range(1, 81)}
    record.save()
    FplSnapshot.objects.bulk_create(
        [
            FplSnapshot(
                key=f"element:2026/27:{pid}",
                payload={
                    "history": [{"minutes": 90, "kickoff_time": "2026-11-01T12:00:00Z"}]
                },
                updated_at=NOW,
                expires_at=NOW + timedelta(hours=1),
            )
            for pid in range(1, 75)
        ]
    )
    with patch("django.utils.timezone.now", return_value=NOW):
        status = history_readiness({"season": "2026/27", "decision_gameweek": 11})
    assert status["candidate_players"] == 40
    assert status["fallback_share_now"] == 0.15
    assert status["coverage_ready"] is False


@pytest.mark.django_db
def test_current_experiment_keeps_priority_when_an_older_worker_updates_v3():
    current = saved_candidates()
    current.version = "forecast-experiment-v4"
    current.save()
    old = saved_candidates()
    old.captured_at = NOW - timedelta(seconds=1)
    old.predictions = {"heuristic-v1": {str(i): float(i) for i in range(1, 41)}}
    old.save()
    with patch("django.utils.timezone.now", return_value=NOW):
        assert priority_candidates(
            {"season": "2026/27", "decision_gameweek": 11}
        ) == set(range(41, 81))
