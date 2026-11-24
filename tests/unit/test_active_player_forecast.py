from copy import deepcopy
from unittest.mock import patch

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.template.loader import render_to_string
from django.test import override_settings

from rivals.services.active_player_forecast import (
    active_model_name,
    build_player_forecast,
)
from rivals.services.calibrated_component_forecast import CalibratedComponentForecast
from rivals.services.decision_engine import DecisionEngine
from rivals.services.landing_data_service import LandingDataService
from rivals.services.team_analysis_service import TeamAnalysisService
from rivals.views.decision_views import planner_pitch_weeks
from tests.unit.test_component_validation import historical_frame
from tests.unit.test_decision_engine import api, confirmed, isolated, manager


@override_settings(FPL_PLAYER_MODEL="heuristic-v1")
def test_default_model_needs_no_history_database_and_unknown_model_fails():
    f = historical_frame()
    with patch(
        "rivals.services.active_player_forecast.read_histories",
        side_effect=AssertionError("standard model needs no histories"),
    ):
        model = build_player_forecast(f["players"], f["fixtures"], 10)
    assert model.version == "heuristic-v1"
    with override_settings(FPL_PLAYER_MODEL="typo"):
        with pytest.raises(ImproperlyConfigured, match="FPL_PLAYER_MODEL"):
            active_model_name()


@override_settings(FPL_PLAYER_MODEL="decision-v12-calibrated")
def test_opt_in_matches_revised_model_and_history_changes_invalidate_identity():
    f = historical_frame()
    histories = deepcopy(f["histories"])
    with patch(
        "rivals.services.active_player_forecast.read_histories", return_value=histories
    ):
        model = build_player_forecast(
            f["players"], f["fixtures"], 10, teams=f["teams"], season=f["season"]
        )
        expected = CalibratedComponentForecast(
            f["players"],
            f["fixtures"],
            10,
            teams=f["teams"],
            histories=histories,
            season=f["season"],
        )
        assert model.points(13, 11) == expected.points(13, 11)
        original_points = model.points(13, 11)
        histories[13][0]["minutes"] = 0
        changed = build_player_forecast(
            f["players"], f["fixtures"], 10, teams=f["teams"], season=f["season"]
        )
        assert changed.cache_identity != model.cache_identity
        assert changed.points(13, 11) != original_points
        next_deadline = build_player_forecast(
            f["players"], f["fixtures"], 11, teams=f["teams"], season=f["season"]
        )
        assert next_deadline.cache_identity != changed.cache_identity


@pytest.mark.django_db
def test_opt_in_recalculates_plans_and_consistently_updates_ui_consumers(manager, api):
    histories = {
        pid: [{"minutes": 90, "kickoff_time": "2026-11-01T12:00:00Z"}]
        for pid in range(1, 46)
    }
    with override_settings(FPL_PLAYER_MODEL="heuristic-v1"):
        standard = DecisionEngine(manager, api=api).load()
        inputs = confirmed(standard)
        original = standard.report(inputs)
    with (
        override_settings(FPL_PLAYER_MODEL="decision-v12-calibrated"),
        patch(
            "rivals.services.active_player_forecast.read_histories",
            return_value=histories,
        ) as reads,
    ):
        revised = DecisionEngine(manager, api=api).load()
        updated = revised.report(inputs)
        assert updated["fingerprint"] != original["fingerprint"]
        assert updated["baseline"]["points"] != original["baseline"]["points"]
        assert updated["model"]["experimental"] is True
        preview = revised.preview()
        assert preview["model"]["player_model"] == "decision-v12-calibrated"
        html = render_to_string(
            "partials/planner_pitch.html",
            {"report": preview, "planner_weeks": planner_pitch_weeks(preview)},
        )
        assert "Revised component model" in html
        landing = LandingDataService()
        landing.fpl_api = api
        assert landing.get_best_xi()["expected_points"] == pytest.approx(
            revised.best_xi(11)["points"]
        )
        analysis = TeamAnalysisService(api=api)
        assert analysis.refresh_data()
        before = reads.call_count
        for player in api.fetch_bootstrap_static.return_value["elements"][:15]:
            points = analysis.calculate_expected_points(player, weeks=3)
            assert points == pytest.approx(
                sum(
                    revised.projections[player["id"]][week]
                    for week in revised.weeks[:3]
                )
            )
        assert reads.call_count == before + 1
