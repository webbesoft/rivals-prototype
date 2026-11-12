from collections import Counter
from itertools import combinations
from unittest.mock import Mock, patch

import pytest
from django.contrib.auth.models import AnonymousUser
from django.template.loader import render_to_string
from django.test import RequestFactory

from core.views import index
from rivals.services.landing_data_service import LandingDataService


@pytest.fixture
def service():
    with patch("rivals.services.landing_data_service.cache") as mock_cache:
        mock_cache.get.return_value = None
        service = LandingDataService()
        service.fpl_api = Mock()
        yield service


def snapshot():
    # Strong players across positions at club 1 make greedy selection suboptimal.
    players = []
    for position, scores in enumerate(
        ([80, 79], [100, 99, 98, 60, 59, 58], [120, 119, 80, 79, 78], [110, 70, 69]),
        start=1,
    ):
        for offset, score in enumerate(scores):
            player_id = len(players) + 1
            players.append(
                {
                    "id": player_id,
                    "web_name": f"Player {player_id}",
                    "team": 1 if offset == 0 else offset + 1,
                    "element_type": position,
                    "minutes": 900,
                    "total_points": 200 - score,
                    "ep_next": str(score / 10),
                    "now_cost": 55 + player_id,
                }
            )
    return {
        "elements": players,
        "teams": [{"id": i, "short_name": f"T{i}"} for i in range(1, 7)],
    }


def test_best_xi_matches_exhaustive_optimum(service):
    data = snapshot()
    service.fpl_api.fetch_bootstrap_static.return_value = data
    result = service.get_best_xi()
    valid_totals = []
    for xi in combinations(data["elements"], 11):
        positions = Counter(p["element_type"] for p in xi)
        clubs = Counter(p["team"] for p in xi)
        if (
            positions[1] == 1
            and 3 <= positions[2] <= 5
            and 2 <= positions[3] <= 5
            and 1 <= positions[4] <= 3
            and max(clubs.values()) <= 3
        ):
            valid_totals.append(sum(float(p["ep_next"]) for p in xi))
    assert float(result["expected_points"]) == pytest.approx(max(valid_totals))
    selected = [p for row in result["rows"] for p in row["players"]]
    assert len({p["id"] for p in selected}) == 11
    assert max(Counter(p["team"] for p in selected).values()) <= 3
    assert (
        result["cost"]
        == sum(
            p["now_cost"]
            for p in data["elements"]
            if p["id"] in {p["id"] for p in selected}
        )
        / 10
    )


@pytest.mark.parametrize("data", [None, {}, {"elements": [], "teams": []}])
def test_missing_data_does_not_produce_a_partial_xi(service, data):
    service.fpl_api.fetch_bootstrap_static.return_value = data
    assert service.get_best_xi() is None


def test_zero_projections_do_not_show_a_zero_point_xi(service):
    data = snapshot()
    for player in data["elements"]:
        player["ep_next"] = "0.0"
    service.fpl_api.fetch_bootstrap_static.return_value = data
    assert service.get_best_xi() is None


def test_cached_xi_avoids_api_call(service):
    with patch("rivals.services.landing_data_service.cache") as mock_cache:
        mock_cache.get.return_value = {"expected_points": 12.3}
        assert service.get_best_xi() == {"expected_points": 12.3}
    service.fpl_api.fetch_bootstrap_static.assert_not_called()


@pytest.mark.parametrize("projection", [None, "", "invalid", "NaN", "Infinity"])
def test_unavailable_projections_do_not_fall_back_to_season_points(service, projection):
    data = snapshot()
    for player in data["elements"]:
        player["ep_next"] = projection
    service.fpl_api.fetch_bootstrap_static.return_value = data
    assert service.get_best_xi() is None


def test_players_without_past_minutes_can_be_selected_on_projections(service):
    data = snapshot()
    for player in data["elements"]:
        player["minutes"] = 0
    service.fpl_api.fetch_bootstrap_static.return_value = data
    assert service.get_best_xi() is not None


def test_landing_renders_xi_and_methodology(service):
    service.fpl_api.fetch_bootstrap_static.return_value = snapshot()
    best_xi = service.get_best_xi()
    request = RequestFactory().get("/")
    request.user = AnonymousUser()
    # Disable context processors so this public template check needs no database.
    html = render_to_string(
        "pages/index.html", {"best_xi": best_xi, "user": request.user}
    )
    assert "The statistically best XI" in html
    assert "No budget cap" in html
    assert "Next gameweek · FPL expected points" in html
    assert "xPts" in html
    assert "season-to-date" not in html
    for row in best_xi["rows"]:
        for player in row["players"]:
            assert f'<strong>{player["name"]}</strong>' in html
    empty_html = render_to_string("pages/index.html", {"user": request.user})
    assert "The best XI will appear" in empty_html


def test_landing_view_passes_xi_to_template():
    with (
        patch("core.views.LandingDataService") as service_class,
        patch("core.views.render") as render,
    ):
        index(RequestFactory().get("/"))
    assert (
        render.call_args.args[2]["best_xi"]
        == service_class.return_value.get_best_xi.return_value
    )
