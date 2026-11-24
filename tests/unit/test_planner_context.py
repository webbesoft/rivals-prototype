from collections import Counter
from unittest.mock import patch

import pytest
from django.template.loader import render_to_string

from rivals.services.decision_engine import DecisionEngine
from rivals.services.landing_data_service import LandingDataService
from rivals.services.planner_presentation import planner_pitch_weeks
from rivals.services.xi_benchmark import best_projected_xi
from tests.unit.test_decision_engine import api, confirmed, isolated, manager
from tests.unit.test_planned_lineup import planned


def test_score_context_matches_public_best_xi_and_full_planner(manager, api):
    engine = DecisionEngine(manager, api).load()
    preview = engine.preview()
    report = engine.report()
    assert preview["benchmarks"] == report["benchmarks"]
    landing = LandingDataService()
    landing.fpl_api = api
    assert preview["benchmarks"][11]["points"] == pytest.approx(
        landing.get_best_xi()["expected_points"]
    )
    for week in planner_pitch_weeks(preview):
        benchmark = preview["benchmarks"][week["gameweek"]]
        selected = [engine.players[pid] for pid in benchmark["ids"]]
        shape = Counter(p["element_type"] for p in selected)
        assert len(selected) == 11 and shape[1] == 1
        assert 3 <= shape[2] <= 5 and 2 <= shape[3] <= 5 and 1 <= shape[4] <= 3
        assert max(Counter(p["team"] for p in selected).values()) <= 3
        expected = sum(engine.projections[p["id"]][week["gameweek"]] for p in selected)
        expected += engine.projections[benchmark["captain"]][week["gameweek"]]
        assert benchmark["points"] == pytest.approx(expected)
        assert week["benchmark"]["points"] >= week["points"]
        assert week["benchmark"]["gap"] == round(
            week["benchmark"]["points"] - week["points"], 1
        )
        assert week["benchmark"]["gap"] == pytest.approx(
            expected - week["points"], abs=0.1
        )


def test_benchmark_cache_shares_work_and_reacts_to_changed_forecasts(manager, api):
    engine = DecisionEngine(manager, api).load()
    engine.prepare(confirmed(engine))
    with patch(
        "rivals.services.decision_engine.best_projected_xi", wraps=best_projected_xi
    ) as select:
        first = engine.best_xi(11)
        other = DecisionEngine(manager, api).load()
        other.prepare(confirmed(other, lineup=planned()))
        assert other.best_xi(11) == first
        assert select.call_count == 1
        captain = first["captain"]
        other.projections[captain][11] += 5
        assert other.best_xi(11)["points"] > first["points"]
        assert select.call_count == 2
        other.players[captain]["removed"] = True
        assert captain not in other.best_xi(11)["ids"]
        assert select.call_count == 3


@pytest.mark.parametrize("missing_position", [False, True])
def test_unavailable_or_zero_benchmark_does_not_invent_a_comparison(
    manager, api, missing_position
):
    engine = DecisionEngine(manager, api).load()
    engine.prepare(confirmed(engine))
    baseline = engine.evaluate(engine.ids)
    for pid, player in engine.players.items():
        if missing_position:
            player["removed"] = player["element_type"] == 1
        else:
            engine.projections[pid] = dict.fromkeys(engine.weeks, 0)
    report = {
        "baseline": baseline,
        "players": engine.player_forecasts(),
        "benchmarks": {week: engine.best_xi(week) for week in engine.weeks},
    }
    weeks = planner_pitch_weeks(report)
    assert all(week["benchmark"] is None for week in weeks)
    html = render_to_string(
        "partials/planner_pitch.html", {"report": report, "planner_weeks": weeks}
    )
    assert "Benchmark unavailable" in html
    assert "<meter" not in html


def test_planned_lineup_score_uses_its_captain_against_the_same_ceiling(manager, api):
    engine = DecisionEngine(manager, api).load()
    report = engine.report(confirmed(engine, lineup=planned()))
    first = planner_pitch_weeks(report)[0]
    score = sum(engine.projections[pid][11] for pid in planned()["xi"])
    score += engine.projections[planned()["captain"]][11]
    assert first["points"] == pytest.approx(score, abs=0.05)
    assert first["benchmark"]["gap"] == pytest.approx(
        report["benchmarks"][11]["points"] - score, abs=0.1
    )
