from copy import deepcopy
from datetime import datetime, timedelta, timezone
from itertools import combinations
from unittest.mock import Mock, patch

import pytest

from rivals.models import GameweekForecast
from rivals.services.component_forecast import ComponentForecast, poisson_floor
from rivals.services.forecast_backtest import (
    run_backtest,
    validate_frame,
    vaastav_frames,
)
from rivals.services.forecast_evaluation import evaluate, select_xi, spearman
from rivals.services.gameweek_forecast_service import (
    forecasts,
    prepare_forecast,
    publish_forecast,
    review_forecasts,
)
from rivals.services.player_forecast import PlayerForecast

NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)


def frame():
    types = [1, 1] + [2] * 5 + [3] * 5 + [4] * 3
    players = [
        {
            "id": i,
            "team": 1 + i % 5,
            "element_type": pos,
            "minutes": 900,
            "form": 3 + i / 10,
            "points_per_game": 4,
            "ep_next": str(4 + i / 10),
            "expected_goals": 2,
            "expected_assists": 1,
            "status": "a",
        }
        for i, pos in enumerate(types, 1)
    ]
    return {
        "season": "2026/27",
        "gameweek": 11,
        "scoring_gameweek": 10,
        "captured_at": NOW.isoformat(),
        "deadline": (NOW + timedelta(days=3)).isoformat(),
        "players": players,
        "fixtures": [
            {
                "id": club,
                "event": 11,
                "team_h": club,
                "team_a": 100,
                "team_h_difficulty": 2,
                "team_a_difficulty": 4,
            }
            for club in range(1, 6)
        ],
        "actual": {p["id"]: p["id"] % 8 for p in players},
    }


def bootstrap(f):
    return {
        "elements": f["players"],
        "teams": [],
        "events": [
            {"id": 1, "deadline_time": "2026-08-01T12:00:00Z"},
            {"id": 10, "deadline_time": "2026-11-07T12:00:00Z"},
            {"id": 11, "deadline_time": f["deadline"]},
        ],
    }


def test_shared_formula_preserves_heuristic_and_doubles_blanks():
    f = frame()
    f["players"][0].update(
        form=5, points_per_game=3, chance_of_playing_next_round=75, minutes=350
    )
    model = PlayerForecast(f["players"], f["fixtures"], 10)
    expected = (0.6 * 5 + 0.4 * 3) * 1.16 * 0.5 * 0.75
    assert model.points(1, 11) == pytest.approx(expected)
    assert model.points(1, 12) == 0
    f["fixtures"].append(dict(f["fixtures"][1]))
    assert PlayerForecast(f["players"], f["fixtures"], 10).points(
        1, 11
    ) == pytest.approx(expected * 2)


def test_rank_ties_and_constant_vectors():
    assert spearman([1, 2, 2, 3], [3, 2, 2, 1]) == pytest.approx(-1)
    assert spearman([1, 1], [2, 3]) is None


def test_captain_xi_is_exact_and_negative_actual_points_are_preserved():
    f = frame()
    predicted = {p["id"]: p["form"] for p in f["players"]}
    selected = select_xi(f["players"], predicted)
    totals = []
    for xi in combinations(f["players"], 11):
        from collections import Counter

        shape = Counter(p["element_type"] for p in xi)
        clubs = Counter(p["team"] for p in xi)
        if (
            shape[1] == 1
            and 3 <= shape[2] <= 5
            and 2 <= shape[3] <= 5
            and 1 <= shape[4] <= 3
            and max(clubs.values()) <= 3
        ):
            totals.append(
                sum(predicted[p["id"]] for p in xi)
                + max(predicted[p["id"]] for p in xi)
            )
    assert selected["points"] == pytest.approx(max(totals))
    actual = {p["id"]: -2 for p in f["players"]}
    metrics = evaluate(f["players"], predicted, actual)
    assert metrics["total_actual"] == -24
    assert metrics["regret"] == 0
    with pytest.raises(ValueError, match="Complete"):
        evaluate(f["players"], predicted, {})


def test_future_inputs_and_duplicate_weeks_rejected():
    f = frame()
    f["captured_at"] = f["deadline"]
    with pytest.raises(ValueError, match="before"):
        validate_frame(f)
    f = frame()
    f["histories"] = {1: [{"kickoff_time": f["deadline"]}]}
    with pytest.raises(ValueError, match="future"):
        validate_frame(f)
    with pytest.raises(ValueError, match="one pre-deadline"):
        run_backtest([frame(), frame()])
    with pytest.raises(ValueError, match="disjoint"):
        run_backtest([], train_seasons=["2024-25"], holdout_seasons=["2024-25"])


def test_prediction_does_not_consume_target_scores_or_xp():
    f = frame()
    before = forecasts(f)
    f["actual"] = dict.fromkeys(f["actual"], 9999)
    for p in f["players"]:
        p["xP"] = 9999
    assert forecasts(f) == before
    f["players"][0]["ep_next"] = None
    result = run_backtest([f])
    assert result["weeks"][0]["metrics"]["ep_next"]["players"] == 14
    assert result["weeks"][0]["common_cohort_metrics"]["heuristic-v1"]["players"] == 14


def test_component_opponent_attack_and_defence_are_separate():
    f = frame()
    teams = [
        {
            "id": club,
            "strength_attack_home": 1000,
            "strength_defence_home": 1000,
            "strength_attack_away": 1000,
            "strength_defence_away": 1000,
        }
        for club in [1, 2, 3, 4, 5, 100]
    ]
    base = ComponentForecast(f["players"], f["fixtures"], 10, teams=teams).components(
        3, 11
    )
    teams[-1]["strength_attack_away"] = 2000
    attacked = ComponentForecast(
        f["players"], f["fixtures"], 10, teams=teams
    ).components(3, 11)
    assert attacked["components"]["goals"] == base["components"]["goals"]
    assert attacked["components"]["clean_sheet"] < base["components"]["clean_sheet"]
    teams[-1]["strength_defence_away"] = 2000
    defended = ComponentForecast(
        f["players"], f["fixtures"], 10, teams=teams
    ).components(3, 11)
    assert defended["components"]["goals"] < attacked["components"]["goals"]


def test_recent_minutes_override_season_discount_and_status_applies_once():
    f = frame()
    f["players"][0]["minutes"] = 90
    history = {
        1: [{"minutes": 90, "starts": 1, "kickoff_time": "2026-11-01T12:00:00Z"}]
    }
    model = ComponentForecast(f["players"], f["fixtures"], 10, histories=history)
    assert model.minutes(1)["expected"] == 90
    assert model.minutes(1, double=True)["expected"] == 81
    f["players"][0]["chance_of_playing_next_round"] = 50
    assert model.minutes(1)["expected"] == 45
    f["players"][0]["status"] = "i"
    assert model.points(1, 11) == 0
    assert poisson_floor(3, 3) < 1  # thresholds, not E[saves]/3


@pytest.mark.django_db
def test_latest_forecast_freezes_at_deadline_and_results_require_final_checks():
    f = frame()
    data = bootstrap(f)
    with patch("django.utils.timezone.now", return_value=NOW):
        prepared = prepare_forecast(data, f["fixtures"])
        assert publish_forecast(prepared)
    record = GameweekForecast.objects.get()
    original = deepcopy(record.predictions)
    prepared["predictions"]["form"][1] = 100
    with patch("django.utils.timezone.now", return_value=NOW + timedelta(days=4)):
        assert not publish_forecast(prepared)
    record.refresh_from_db()
    assert record.predictions == original
    api = Mock()
    data["events"][-1].update(finished=True, data_checked=False)
    api.fetch_bootstrap_static.return_value = data
    assert review_forecasts(api) == 0
    api.fetch_gameweek_live.assert_not_called()
    data["events"][-1]["data_checked"] = True
    api.fetch_gameweek_live.return_value = {
        "elements": [
            {"id": pid, "stats": {"total_points": points}}
            for pid, points in f["actual"].items()
        ]
    }
    assert review_forecasts(api) == 1
    assert review_forecasts(api) == 0
    record.refresh_from_db()
    assert record.outcomes["metrics"]["heuristic-v1"]["regret"] >= 0


def test_ridge_uses_training_seasons_only():
    from rivals.services.forecast_ridge import fit_ridge

    training = frame()
    training["season"] = "2024-25"
    # Enough observations in each position for a regularised fit.
    artifact = fit_ridge([training] * 10)
    changed = deepcopy(training)
    changed["actual"] = dict.fromkeys(changed["actual"], 100)
    assert artifact != fit_ridge([changed] * 10)
    with pytest.raises(ValueError, match="precede"):
        run_backtest(
            [], train_seasons=["2025-26"], holdout_seasons=["2024-25"], fit=True
        )


def test_historical_adapter_lags_inputs_and_excludes_xp(tmp_path):
    import csv

    root = tmp_path / "data" / "2023-24" / "gws"
    root.mkdir(parents=True)
    fields = [
        "element",
        "GW",
        "kickoff_time",
        "minutes",
        "total_points",
        "xP",
        "position",
        "team",
        "fixture",
        "was_home",
    ]
    rows = [
        dict(
            zip(
                fields,
                [
                    1,
                    week,
                    f"2023-08-{day}T12:00:00Z",
                    90,
                    points,
                    99999,
                    "GK",
                    "A",
                    week,
                    "True",
                ],
            )
        )
        for week, day, points in [(1, "01", 2), (2, "08", 50)]
    ]
    with (root / "merged_gw.csv").open("w") as stream:
        writer = csv.DictWriter(stream, fields)
        writer.writeheader()
        writer.writerows(rows)
    frames = vaastav_frames(tmp_path, "2023-24")
    assert len(frames) == 1
    assert frames[0]["players"][0]["points_per_game"] == 2
    assert frames[0]["actual"][1] == 50
    assert "ep_next" not in frames[0]["players"][0]


@pytest.mark.django_db
def test_history_worker_is_bounded_and_expired_histories_are_excluded():
    from rivals.models import FplSnapshot
    from rivals.services.forecast_history_service import (
        read_histories,
        refresh_histories,
    )

    api = Mock()
    api.BASE_URL = "https://example.test/api"
    api.fetch_bootstrap_static.return_value = {
        "elements": [{"id": i} for i in range(50)],
        "events": bootstrap(frame())["events"],
    }
    api.make_request.return_value = {
        "history": [{"kickoff_time": "2026-11-01T12:00:00Z", "minutes": 90}]
    }
    with patch("django.utils.timezone.now", return_value=NOW):
        result = refresh_histories(api, {"season": "2026/27"})
        assert len(result) == 20
        assert api.make_request.call_count == 20
        for i, expiry in [(1, -1), (2, 1)]:
            FplSnapshot.objects.create(
                key=f"element:2026/27:{i}",
                payload=api.make_request.return_value,
                updated_at=NOW,
                expires_at=NOW + timedelta(hours=expiry),
            )
        assert set(read_histories("2026/27")) == {2}
        assert read_histories("2025/26") == {}


@pytest.mark.django_db
def test_public_accuracy_table_renders_frozen_results():
    from django.template.loader import render_to_string

    f = frame()
    with patch("django.utils.timezone.now", return_value=NOW):
        assert publish_forecast(prepare_forecast(bootstrap(f), f["fixtures"]))
    record = GameweekForecast.objects.get()
    record.outcomes = {
        "metrics": {
            "heuristic-v1": {
                "total_predicted": 55.5,
                "total_actual": 60,
                "hindsight_total": 120,
                "xi_overlap": 4,
            }
        }
    }
    html = render_to_string("pages/index.html", {"forecast_history": [record]})
    assert "How our predictions performed" in html
    assert "55.5" in html and "4/11" in html


def test_team_analysis_uses_exact_week_not_five_week_average():
    from rivals.services.team_analysis_service import TeamAnalysisService

    f = frame()
    service = TeamAnalysisService()
    service.current_gw, service.decision_gw = 10, 11
    service.fixtures = f["fixtures"]
    player = f["players"][0]
    shared = PlayerForecast(f["players"], f["fixtures"], 10)
    assert service.calculate_expected_points(player, weeks=1) == shared.points(
        player["id"], 11
    )
    assert service.calculate_expected_points(player, weeks=5) == shared.points(
        player["id"], 11
    )


@pytest.mark.django_db
def test_incomplete_scores_and_different_season_stay_pending():
    f = frame()
    data = bootstrap(f)
    with patch("django.utils.timezone.now", return_value=NOW):
        publish_forecast(prepare_forecast(data, f["fixtures"]))
    data["events"][-1].update(finished=True, data_checked=True)
    api = Mock()
    api.fetch_bootstrap_static.return_value = data
    api.fetch_gameweek_live.return_value = {
        "elements": [{"id": 1, "stats": {"total_points": 0}}]
    }
    assert review_forecasts(api) == 0
    assert GameweekForecast.objects.get().outcomes == {}
    api.fetch_gameweek_live.reset_mock()
    for event in data["events"]:
        event["deadline_time"] = event["deadline_time"].replace("2026", "2027")
    assert review_forecasts(api) == 0
    api.fetch_gameweek_live.assert_not_called()


def test_candidate_deviance_reports_negative_scores_and_prediction_floor():
    from rivals.services.forecast_evaluation import candidate_scores

    scores = candidate_scores({1: 2, 2: -1, 3: 2}, {1: 2, 2: 0, 3: -2}, {1, 2, 3})
    assert scores["rmse"] == pytest.approx((17 / 3) ** 0.5)
    assert scores["poisson_n"] == 2
    assert scores["negative_actuals_excluded_from_deviance"] == 1
    assert scores["predictions_floored_for_deviance"] == 1
    assert scores["poisson_deviance"] == pytest.approx(1e-6)
    assert scores["calibration"]["2"]["actual"] == 0


def test_top_candidates_use_forecasts_and_common_union_not_actual_scores():
    from rivals.services.forecast_evaluation import evaluate_models

    f = frame()
    old = {p["id"]: float(p["id"]) for p in f["players"]}
    new = {pid: -value for pid, value in old.items()}
    result = evaluate_models(
        f["players"], {"heuristic-v1": old, "decision-v11": new}, f["actual"], top_k=3
    )
    assert result["metrics"]["heuristic-v1"]["top_candidates"]["ids"] == [13, 14, 15]
    assert result["metrics"]["decision-v11"]["top_candidates"]["ids"] == [1, 2, 3]
    pair = result["metrics"]["decision-v11"]["paired_candidate_pool"]
    assert pair["model"]["ids"] == pair["reference"]["ids"] == [1, 2, 3, 13, 14, 15]
    old_actual = result["metrics"]["heuristic-v1"]["total_actual"]
    assert (
        result["paired_differences"]["decision-v11"]["total_actual"]
        == result["metrics"]["decision-v11"]["total_actual"] - old_actual
    )
    with pytest.raises(ValueError):
        evaluate_models(f["players"], {"heuristic-v1": old}, f["actual"], top_k=0)


def test_paired_bootstrap_uses_gameweeks_and_preserves_zero_difference():
    from rivals.services.forecast_evaluation import bootstrap_paired

    zero = bootstrap_paired([0] * 10, samples=1000)
    assert zero["interval_95"] == [0, 0] and zero["includes_zero"]
    assert bootstrap_paired([3] * 10, samples=1000)["interval_95"] == [3, 3]
    noisy = bootstrap_paired([-10, 10] * 5, samples=1000)
    assert noisy["mean"] == 0 and noisy["interval_95"][0] < 0 < noisy["interval_95"][1]
    assert noisy == bootstrap_paired([-10, 10] * 5, samples=1000)
    assert noisy["resampling_unit"] == "gameweek"
    assert bootstrap_paired([2])["interval_95"] is None


def test_rolling_team_strengths_deduplicate_matches_and_use_only_prior_inputs():
    from rivals.services.historical_forecast_features import (
        rolling_strengths,
        fixture_proxies,
    )

    row = {
        "team": "A",
        "fixture": "1",
        "kickoff_time": "2023-08-01T12:00:00Z",
        "was_home": "True",
        "team_h_score": "5",
        "team_a_score": "0",
    }
    single = rolling_strengths([row], {"A", "B"})
    duplicated = rolling_strengths([row] * 22, {"A", "B"})
    assert single == duplicated
    assert single[0]["strength_attack_home"] > 1
    assert single[0]["strength_defence_home"] > 1
    fix = fixture_proxies([{"team_h": "B", "team_a": "A"}], single)
    assert 1 <= fix[0]["team_a_difficulty"] <= 5


def test_historical_availability_is_applied_once_in_component_model():
    from rivals.services.historical_forecast_features import recent_play_probability

    f = frame()
    history = [
        {"minutes": value, "fixture": i, "kickoff_time": f"2026-11-0{i+1}T12:00:00Z"}
        for i, value in enumerate([90, 0, 90, 0])
    ]
    probability = recent_play_probability(history)
    p = f["players"][0]
    p.update(
        chance_of_playing_next_round=100 * probability,
        availability_source="recent-match-proxy",
    )
    model = ComponentForecast(f["players"], f["fixtures"], 10, histories={1: history})
    assert model.minutes(1)["play"] == pytest.approx(probability)
    assert model.minutes(1)["expected"] == pytest.approx(90 * probability)


@pytest.mark.django_db
def test_v11_shadow_freezes_raw_bootstrap_and_preserves_default():
    f = frame()
    b = bootstrap(f)
    with patch("django.utils.timezone.now", return_value=NOW):
        prepared = prepare_forecast(b, f["fixtures"])
        assert prepared["frame"]["bootstrap"] == b
        assert prepared["frame"]["model_versions"]["shadow"] == "decision-v11"
        assert "decision-v11" in prepared["predictions"]
        assert "decision-v11" in prepared["selections"]
        assert prepared["predictions"]["heuristic-v1"] == forecasts(f)["heuristic-v1"]
        publish_forecast(prepared)
    record = GameweekForecast.objects.get()
    assert record.version == "forecast-experiment-v3"
    assert (
        record.inputs["bootstrap"]["elements"][0]["ep_next"]
        == b["elements"][0]["ep_next"]
    )


def test_csv_team_strengths_ignore_target_scores_and_audit_missing_labels(tmp_path):
    import csv

    root = tmp_path / "data" / "2023-24" / "gws"
    root.mkdir(parents=True)
    fields = [
        "element",
        "GW",
        "kickoff_time",
        "minutes",
        "total_points",
        "position",
        "team",
        "fixture",
        "was_home",
        "team_h_score",
        "team_a_score",
    ]
    rows = []
    for week, day, home_score in [(1, "01", 2), (2, "08", 99), (3, "15", 7)]:
        for pid, team, home in [(1, "A", True), (2, "B", False)]:
            if week == 3 and pid == 2:
                continue
            rows.append(
                dict(
                    zip(
                        fields,
                        [
                            pid,
                            week,
                            f"2023-08-{day}T12:00:00Z",
                            90,
                            2,
                            "GK" if home else "FWD",
                            team,
                            week,
                            str(home),
                            home_score,
                            0,
                        ],
                    )
                )
            )
    with (root / "merged_gw.csv").open("w") as stream:
        writer = csv.DictWriter(stream, fields)
        writer.writeheader()
        writer.writerows(rows)
    audit = []
    frames = vaastav_frames(tmp_path, "2023-24", audit=audit)
    assert len(frames) == 1 and frames[0]["gameweek"] == 2
    team = next(t for t in frames[0]["teams"] if t["id"] == "A")
    assert team["strength_attack_home"] == pytest.approx(((2 + 5 * 1.5) / 6) / 1.5)
    assert all(p["chance_of_playing_next_round"] == 100 for p in frames[0]["players"])
    dropped = next(row for row in audit if row["gameweek"] == 3)
    assert dropped["reason"] == "missing_labels" and dropped["missing_player_ids"] == [
        2
    ]
    assert (
        next(row for row in audit if row["gameweek"] == 1)["reason"]
        == "no_prior_player_pool"
    )


def test_calibration_aggregates_observation_counts_not_weekly_means():
    from rivals.services.forecast_evaluation import (
        aggregate_candidates,
        candidate_scores,
    )

    first = candidate_scores({1: 0.1, 2: 0.1}, {1: 0, 2: 0}, {1, 2})
    second = candidate_scores({3: 0.9}, {3: 6}, {3})
    result = aggregate_candidates([first, second])
    assert result["calibration"]["0"]["n"] == 3
    assert result["calibration"]["0"]["predicted"] == pytest.approx(1.1 / 3)
    assert result["calibration"]["0"]["actual"] == 2


@pytest.mark.django_db
def test_shadow_summary_command_handles_zero_completed_weeks():
    import io, json
    from django.core.management import call_command

    output = io.StringIO()
    call_command("report_forecasts", season="2026/27", summary=True, stdout=output)
    report = json.loads(output.getvalue())
    assert report["completed_gameweeks"] == 0
    assert report["status"] == "collecting"
    assert report["paired_vs_heuristic"] == {}


def test_ridge_standardises_training_features_and_does_not_shrink_intercept():
    from rivals.services.forecast_ridge import fit_ridge, predict_ridge, FEATURES

    f = frame()
    f["players"] = [
        {"id": pos * 100 + i, "element_type": pos, "team": 1}
        for pos in (1, 2, 3, 4)
        for i in range(16)
    ]
    f["actual"] = {p["id"]: 3 * (p["id"] % 100) for p in f["players"]}

    def fake_features(model, pid, week):
        return [1.0, float(pid % 100), *([0.0] * (len(FEATURES) - 1))]

    with (
        patch(
            "rivals.services.forecast_ridge.component_model",
            return_value=Mock(fixtures={(1, 11): [{}]}),
        ),
        patch("rivals.services.forecast_ridge.features", side_effect=fake_features),
    ):
        artifact = fit_ridge([f])
        transform = artifact["standardization"]["1"]
        assert transform["means"][0] == 7.5
        assert transform["scales"][0] == pytest.approx(
            (sum((i - 7.5) ** 2 for i in range(16)) / 16) ** 0.5
        )
        assert transform["scales"][1:] == [1.0] * (len(FEATURES) - 1)
        # z is centred, so the unpenalised intercept equals the residual mean.
        assert artifact["standardized_corrections"]["1"][0] == pytest.approx(15)
        expected = 2 * 16 * transform["scales"][0] / (16 + 100)
        assert artifact["standardized_corrections"]["1"][1] == pytest.approx(expected)
        prediction = predict_ridge(f, artifact)
        for p in f["players"]:
            raw = [1.0, float(p["id"] % 100), *([0.0] * (len(FEATURES) - 1))]
            assert prediction[p["id"]] == pytest.approx(
                sum(
                    a * b
                    for a, b in zip(
                        raw, artifact["coefficients"][str(p["element_type"])]
                    )
                )
            )
        assert not artifact["live_enabled"]
        with pytest.raises(ValueError, match="refitted"):
            predict_ridge(f, {"coefficients": artifact["coefficients"]})


def test_history_strata_preserve_frozen_xi_contributions_and_candidate_pool():
    from rivals.services.forecast_evaluation import (
        evaluate_models,
        summarise_experiments,
    )

    f = frame()
    f["histories"] = {i: [{"minutes": 90}] for i in range(1, 8)}
    predictions = forecasts(f)
    result = evaluate_models(
        f["players"], predictions, f["actual"], fixtures=f["fixtures"], week=11, frame=f
    )
    for values in result["metrics"].values():
        strata = values["history_strata"]
        assert set(strata) == {"has_history", "fallback"}
        for metric in ("xi_actual", "captain_actual", "total_actual"):
            assert sum(group[metric] for group in strata.values()) == values[metric]
        assert sum(group["players"] for group in strata.values()) == values["players"]
        assert values["coverage"]["fallback_share"] == pytest.approx(8 / 15)
    for model, difference in result["paired_differences"].items():
        assert (
            sum(
                group["total_actual"]
                for group in result["paired_history_differences"][model].values()
            )
            == difference["total_actual"]
        )
    report = summarise_experiments([result])
    assert (
        report["history_strata"]["fallback"]["diagnostics"]["decision-v11"][
            "whole_pool_player_gameweeks"
        ]
        == 8
    )
    errors = report["paired_candidate_pools"]["decision-v11"][
        "paired_error_differences"
    ]
    assert errors["rmse"]["gameweeks"] == 1
    assert errors["rmse"]["interval_95"] is None


def test_pooled_calibration_slope_and_clean_sheet_diagnostic_use_frozen_components():
    from rivals.services.forecast_diagnostics import moments, calibration
    from rivals.services.forecast_evaluation import (
        evaluate_models,
        summarise_experiments,
    )

    fitted = calibration([moments({1: 1, 2: 2, 3: 3}, {1: 3, 2: 5, 3: 7}, {1, 2, 3})])
    assert fitted["slope"] == 2 and fitted["intercept"] == 1
    f = frame()
    f["actual_clean_sheets"] = {p["id"]: 1 for p in f["players"]}
    prepared = prepare_forecast(bootstrap(f), f["fixtures"], captured_at=NOW)
    f["challenger_details"] = prepared["frame"]["challenger_details"]
    # Deliberately change a frozen prediction to prove diagnostics don't refit it.
    f["challenger_details"][1]["components"]["clean_sheet"] = 4.0
    result = evaluate_models(
        f["players"],
        forecasts(f),
        f["actual"],
        fixtures=f["fixtures"],
        week=11,
        frame=f,
    )
    cs = result["metrics"]["decision-v11"]["clean_sheet_calibration"]
    assert cs["position"]["1"]["n"] == 2
    assert cs["position"]["1"]["sum_predicted"] >= 1
    report = summarise_experiments([result])
    assert (
        report["clean_sheet_calibration"]["decision-v11"]["position"]["1"]["actual"]
        == 1
    )


@pytest.mark.django_db
def test_fallback_coverage_and_policy_are_frozen_before_outcomes():
    f = frame()
    histories = {1: [{"minutes": 90}], 2: [{"minutes": None}]}
    prepared = prepare_forecast(
        bootstrap(f), f["fixtures"], captured_at=NOW, histories=histories
    )
    assert prepared["frame"]["history_coverage"]["fallback_share"] == pytest.approx(
        14 / 15
    )
    assert prepared["frame"]["history_usage"][1]["has_usable_history"]
    assert not prepared["frame"]["history_usage"][2]["has_usable_history"]
    assert (
        prepared["frame"]["promotion_policy"]["minimum_completed_live_gameweeks"] == 10
    )
    assert prepared["frame"]["model_config"]["recency_weight"] == 0.75
    assert len(prepared["frame"]["model_fingerprint"]) == 64
    assert not any("ridge" in key for key in prepared["predictions"])
