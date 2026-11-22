"""Regressions for forecast review findings, including already-safe behaviour."""

from copy import deepcopy
from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from django.core.cache import cache
from django.db import transaction
from django.template.loader import render_to_string

from rivals.models import FplSnapshot, GameweekForecast, SyncState
from rivals.services.component_forecast import ComponentForecast, poisson_floor
from rivals.services.forecast_diagnostics import calibration, moments
from rivals.services.forecast_evaluation import (
    evaluate,
    evaluate_models,
    summarise_experiments,
)
from rivals.services.forecast_history_service import refresh_histories
from rivals.services.forecast_ridge import fit_ridge, predict_ridge
from rivals.services.gameweek_forecast_service import (
    prepare_forecast,
    publish_forecast,
    review_forecasts,
)
from rivals.services.landing_data_service import LandingDataService
from rivals.services.fpl_api_service import FplApiService
from rivals.tasks import refresh_resource
from tests.unit.test_forecast_evaluation import NOW, frame, bootstrap


@pytest.fixture(autouse=True)
def fixed_time():
    cache.clear()
    with patch("django.utils.timezone.now", return_value=NOW):
        yield
    cache.clear()


@pytest.fixture
def prepared():
    f = frame()
    return prepare_forecast(bootstrap(f), f["fixtures"])


def test_threshold_points_weight_playing_probability_after_threshold():
    f = frame()
    p = f["players"][0]
    history = {1: [{"minutes": 90, "saves": 3, "kickoff_time": "2026-11-01T12:00:00Z"}]}
    model = ComponentForecast(f["players"], f["fixtures"], 10, histories=history)
    full = model.components(1, 11)["components"]
    assert full["saves"] == pytest.approx(poisson_floor(3, 3))
    p["chance_of_playing_next_round"] = 50
    half = model.components(1, 11)["components"]
    assert half["saves"] == pytest.approx(full["saves"] / 2)
    assert half["conceded"] == pytest.approx(full["conceded"] / 2)


def test_recent_attacking_rates_do_not_depend_on_history_input_order():
    f = frame()
    history = [
        {
            "minutes": 90,
            "fixture": i,
            "kickoff_time": f"2026-11-{i:02d}T12:00:00Z",
            "expected_goals": i / 10,
        }
        for i in range(1, 9)
    ]
    ordered = ComponentForecast(f["players"], f["fixtures"], 10, histories={1: history})
    reversed_rows = ComponentForecast(
        f["players"], f["fixtures"], 10, histories={1: list(reversed(history))}
    )
    assert ordered.components(1, 11) == reversed_rows.components(1, 11)


def test_calibration_ignores_nonadditive_extra_fields_and_requires_moments():
    raw = moments({1: 1, 2: 2}, {1: 2, 2: 4}, {1, 2})
    augmented = {**raw, "slope": 2, "metadata": "not additive"}
    assert calibration([augmented, raw]) == calibration([raw, raw])
    with pytest.raises(KeyError):
        calibration([{"n": 2}])


def test_model_week_means_do_not_treat_missing_model_weeks_as_zero():
    f = frame()
    result = evaluate_models(
        f["players"],
        {"heuristic-v1": {p["id"]: 2.0 for p in f["players"]}},
        f["actual"],
    )
    report = summarise_experiments([result, {"metrics": {}, "paired_differences": {}}])
    assert (
        report["diagnostics"]["heuristic-v1"]["mean_total_actual"]
        == result["metrics"]["heuristic-v1"]["total_actual"]
    )


@pytest.mark.django_db
def test_pending_history_is_empty_and_scored_history_is_preserved(prepared):
    publish_forecast(prepared)
    context = {"season": frame()["season"]}
    with patch(
        "rivals.services.gameweek_context_service.GameweekContextService.cached",
        return_value=context,
    ):
        assert LandingDataService().get_forecast_history() == []
        html = render_to_string("pages/index.html", {"forecast_history": []})
        assert "How our predictions performed" not in html
        record = GameweekForecast.objects.get()
        record.outcomes = {"metrics": {"heuristic-v1": {"total_actual": 50}}}
        record.save(update_fields=["outcomes"])
        assert (
            LandingDataService().get_forecast_history()[0].outcomes == record.outcomes
        )


@pytest.mark.django_db
def test_fresh_extended_deadline_can_replace_an_unscored_record(prepared):
    publish_forecast(prepared)
    GameweekForecast.objects.update(deadline=NOW - timedelta(minutes=1))
    updated = deepcopy(prepared)
    updated["frame"]["captured_at"] = (NOW + timedelta(seconds=1)).isoformat()
    with patch("django.utils.timezone.now", return_value=NOW + timedelta(seconds=2)):
        assert publish_forecast(updated)
    record = GameweekForecast.objects.get()
    record.outcomes = {"scored": True}
    record.save(update_fields=["outcomes"])
    updated["frame"]["captured_at"] = (NOW + timedelta(seconds=3)).isoformat()
    with patch("django.utils.timezone.now", return_value=NOW + timedelta(seconds=4)):
        assert not publish_forecast(updated)
    record.refresh_from_db()
    assert record.outcomes == {"scored": True}
    assert record.captured_at == NOW + timedelta(seconds=1)


@pytest.mark.django_db
def test_deadline_rechecked_after_write_wait_rolls_back_insert(prepared):
    original = GameweekForecast.objects.get_or_create

    def delayed(**kwargs):
        result = original(**kwargs)
        clock.return_value = NOW + timedelta(days=4)
        return result

    with (
        patch("django.utils.timezone.now", return_value=NOW) as clock,
        patch.object(GameweekForecast.objects, "get_or_create", side_effect=delayed),
    ):
        assert not publish_forecast(prepared)
    assert not GameweekForecast.objects.exists()


def test_finished_gw38_has_no_preparation_deadline():
    b = bootstrap(frame())
    b["events"] = [
        {"id": 1, "deadline_time": "2025-08-01T12:00:00Z"},
        {"id": 38, "deadline_time": "2026-05-20T12:00:00Z", "finished": True},
    ]
    assert prepare_forecast(b, frame()["fixtures"]) is None


@pytest.mark.django_db
def test_selection_roundtrip_preserves_captain_101_and_incomplete_scores_pending():
    f = frame()
    f["players"][0]["id"] = 101
    prepared = prepare_forecast(bootstrap(f), f["fixtures"])
    values = {p["id"]: float(p["id"]) for p in f["players"]}
    from rivals.services.forecast_evaluation import select_xi

    selected = select_xi(f["players"], values)
    assert selected["captain"] == 101
    actual = {p["id"]: 1.0 for p in f["players"]}
    actual[101] = 17.0
    before = evaluate(f["players"], values, actual, selected=selected)
    prepared["predictions"] = {"heuristic-v1": values}
    prepared["selections"] = {"heuristic-v1": selected}
    publish_forecast(prepared)
    record = GameweekForecast.objects.get()
    after = evaluate(
        record.inputs["players"],
        values,
        actual,
        selected=record.selections["heuristic-v1"],
    )
    assert before["total_actual"] == after["total_actual"] == 44.0
    api = Mock()
    b = bootstrap(f)
    b["events"][-1].update(finished=True, data_checked=True)
    api.fetch_bootstrap_static.return_value = b
    api.fetch_gameweek_live.return_value = {
        "elements": [
            {"id": i, "stats": {"total_points": score}}
            for i, score in actual.items()
            if i != 101
        ]
    }
    assert review_forecasts(api) == 0
    record.refresh_from_db()
    assert record.outcomes == {}


def test_supplied_selection_missing_captain_is_rejected_even_without_hindsight_xi():
    p = {"id": 1, "team": 1, "element_type": 1}
    with pytest.raises(ValueError):
        evaluate_models(
            [p],
            {"heuristic-v1": {1: 2}},
            {1: 1},
            selections={"heuristic-v1": {"ids": [1], "captain": 101}},
        )


@pytest.mark.django_db
def test_history_season_mismatch_never_requests_player_ids():
    api = Mock()
    api.fetch_bootstrap_static.return_value = bootstrap(frame())
    with pytest.raises(RuntimeError, match="season"):
        refresh_histories(api, {"season": "2027/28"})
    api.make_request.assert_not_called()


@pytest.mark.django_db
def test_failed_history_requests_back_off_before_request_limit():
    api = Mock()
    b = bootstrap(frame())
    b["elements"] = [{"id": i} for i in range(50)]
    api.fetch_bootstrap_static.return_value = b
    api.BASE_URL = "https://example.test/api"
    api.make_request.return_value = None
    refresh_histories(api, {"season": "2026/27"})
    first = [c.args[0] for c in api.make_request.call_args_list]
    api.make_request.reset_mock()
    with patch("django.utils.timezone.now", return_value=NOW + timedelta(minutes=2)):
        refresh_histories(api, {"season": "2026/27"})
    second = [c.args[0] for c in api.make_request.call_args_list]
    assert len(first) == len(second) == 20
    assert not set(first) & set(second)


def test_ridge_blank_bypasses_intercept():
    f = frame()
    artifact = fit_ridge([f] * 10)
    f["fixtures"] = []
    assert set(predict_ridge(f, artifact).values()) == {0.0}


def worker_api():
    api = Mock(spec=FplApiService)
    api.SEASON_CACHE_KEY = FplApiService.SEASON_CACHE_KEY
    api.SEASON_CACHE_TIMEOUT = FplApiService.SEASON_CACHE_TIMEOUT
    api.fetch_bootstrap_static.return_value = bootstrap(frame())
    api.fetch_fixtures_data.return_value = frame()["fixtures"]
    return api


def stored_context():
    return SyncState.objects.create(
        key="context",
        kind="context",
        status="ok",
        last_success_at=NOW,
        payload={
            "events": bootstrap(frame())["events"],
            "fixtures": frame()["fixtures"],
        },
    )


@pytest.mark.django_db
@pytest.mark.parametrize("failure", ["history", "model", "write"])
def test_forecast_failure_is_retryable_without_rolling_back_core_publications(failure):
    api = worker_api()
    core = SyncState.objects.create(
        key="context", kind="context", status="queued", generation=1
    )
    capture = SyncState.objects.create(
        key="forecasts", kind="forecasts", status="queued", generation=1
    )
    target = {
        "history": "rivals.services.forecast_history_service.read_histories",
        "model": "rivals.services.gameweek_forecast_service.prepare_forecast",
        "write": "rivals.services.gameweek_forecast_service.publish_forecast",
    }[failure]
    with (
        patch("rivals.tasks.FplApiService", return_value=api),
        patch(target, side_effect=RuntimeError("forecast failed")),
    ):
        refresh_resource.now(core.key, core.generation)
        core.refresh_from_db()
        assert core.status == "ok"
        assert (
            FplSnapshot.objects.filter(key__in=["bootstrap", "fixtures"]).count() == 2
        )
        refresh_resource.now(capture.key, capture.generation)
    capture.refresh_from_db()
    core.refresh_from_db()
    assert core.status == "ok" and capture.status == "error"
    assert capture.lease_until is None and capture.next_attempt_at > NOW
    assert FplSnapshot.objects.filter(key__in=["bootstrap", "fixtures"]).count() == 2


@pytest.mark.django_db
def test_forecast_scoring_retries_independently_of_successful_decision_review():
    stored_context()
    decisions = SyncState.objects.create(
        key="decisions", kind="decisions", status="queued", generation=1
    )
    scoring = SyncState.objects.create(
        key="forecast_reviews", kind="forecast_reviews", status="queued", generation=1
    )
    with (
        patch(
            "rivals.services.decision_review_service.review_decisions", return_value=3
        ) as decision_review,
        patch(
            "rivals.services.gameweek_forecast_service.review_forecasts",
            side_effect=[RuntimeError("scoring failed"), 1],
        ) as forecast_review,
    ):
        refresh_resource.now(decisions.key, decisions.generation)
        decisions.refresh_from_db()
        assert decisions.status == "ok" and decisions.payload["reviewed"] == 3
        refresh_resource.now(scoring.key, scoring.generation)
        scoring.refresh_from_db()
        assert scoring.status == "error"
        scoring.generation += 1
        scoring.status = "queued"
        scoring.save()
        refresh_resource.now(scoring.key, scoring.generation)
    assert decision_review.call_count == 1 and forecast_review.call_count == 2
    scoring.refresh_from_db()
    assert scoring.status == "ok"


@pytest.mark.django_db
def test_superseded_scorer_cannot_overwrite_replacement_outcomes(prepared):
    stored_context()
    publish_forecast(prepared)
    record = GameweekForecast.objects.get()
    scoring = SyncState.objects.create(
        key="forecast_reviews", kind="forecast_reviews", status="queued", generation=1
    )
    api = worker_api()
    api.fetch_bootstrap_static.return_value["events"][-1].update(
        finished=True, data_checked=True
    )
    replacement = {"replacement_worker": True}

    def reclaim(*args, **kwargs):
        SyncState.objects.filter(pk=scoring.pk).update(generation=2, status="queued")
        GameweekForecast.objects.filter(pk=record.pk).update(outcomes=replacement)
        return {
            "elements": [
                {"id": pid, "stats": {"total_points": value}}
                for pid, value in frame()["actual"].items()
            ]
        }

    api.fetch_gameweek_live.side_effect = reclaim
    with patch("rivals.tasks.FplApiService", return_value=api):
        refresh_resource.now(scoring.key, 1)
    record.refresh_from_db()
    scoring.refresh_from_db()
    assert record.outcomes == replacement
    assert scoring.generation == 2 and scoring.status == "queued"


@pytest.mark.django_db
def test_final_scoring_bypasses_cached_provisional_live_scores(prepared):
    publish_forecast(prepared)
    api = FplApiService()
    b = bootstrap(frame())
    b["events"][-1].update(finished=True, data_checked=True)
    provisional = {
        "elements": [
            {"id": pid, "stats": {"total_points": 0}} for pid in frame()["actual"]
        ]
    }
    final = {
        "elements": [
            {"id": pid, "stats": {"total_points": value}}
            for pid, value in frame()["actual"].items()
        ]
    }
    cache.set("fpl_live_2026/27_11", provisional, 120)
    with (
        patch.object(api, "fetch_bootstrap_static", return_value=b),
        patch.object(api, "make_request", return_value=final) as transport,
    ):
        assert review_forecasts(api) == 1
    transport.assert_called_once_with(f"{api.BASE_URL}/event/11/live/")
    actual = GameweekForecast.objects.get().outcomes["actual"]
    assert actual["1"] == frame()["actual"][1]
    assert cache.get("fpl_live_2026/27_11") == final


@pytest.mark.django_db
def test_forecast_publication_cannot_be_nested_in_a_later_commit(prepared):
    with transaction.atomic():
        with pytest.raises(RuntimeError, match="durable"):
            publish_forecast(prepared)
    assert not GameweekForecast.objects.exists()


@pytest.mark.django_db
def test_deadline_rechecked_after_owner_lock_wait(prepared):
    with patch("django.utils.timezone.now", return_value=NOW) as clock:

        def guard():
            clock.return_value = NOW + timedelta(days=4)

        assert not publish_forecast(prepared, publication_guard=guard)
    assert not GameweekForecast.objects.exists()


@pytest.mark.django_db
def test_pending_index_renders_without_scored_metrics(prepared):
    from django.test import Client

    stored_context()
    publish_forecast(prepared)
    with patch("requests.get", side_effect=AssertionError("No web request")):
        response = Client().get("/")
    assert response.status_code == 200
    assert "How our predictions performed" not in response.content.decode()


def test_recently_started_history_has_consistent_coverage_and_usage():
    histories = {
        1: [
            {
                "minutes": 1,
                "fixture": 1,
                "kickoff_time": (NOW - timedelta(minutes=1)).isoformat(),
            }
        ]
    }
    prepared = prepare_forecast(
        bootstrap(frame()), frame()["fixtures"], histories=histories
    )
    usage = prepared["frame"]["history_usage"]
    assert usage[1]["has_usable_history"]
    assert prepared["frame"]["history_coverage"]["fallback_players"] == sum(
        not v["has_usable_history"] for v in usage.values()
    )


def test_candidate_coverage_uses_model_versions():
    with patch.object(ComponentForecast, "version", "shadow-test"):
        prepared = prepare_forecast(bootstrap(frame()), frame()["fixtures"])
    assert "shadow-test" in prepared["predictions"]
    assert "shadow-test" in prepared["frame"]["candidate_history_coverage"]
