from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.contrib.auth.models import AnonymousUser
from django.template.loader import render_to_string
from django.test import RequestFactory, override_settings

from rivals.services.team_analysis_service import TeamAnalysisService


def comparison_from_entries(my_entry, rival_entry):
    api = Mock()
    api.fetch_team_picks.return_value = {
        "picks": [],
        "entry_history": {"bank": 0},
    }
    api.fetch_team_basic_info.side_effect = [my_entry, rival_entry]
    service = TeamAnalysisService(api=api)
    service.bootstrap = {"elements": []}
    service.current_gw = 10
    result = service.compare_teams(1180578, 1122816)
    assert "error" not in result
    assert api.fetch_team_basic_info.call_count == 2
    return result


@override_settings(ALLOWED_HOSTS=["testserver"])
def render_scoreboard(comparison):
    request = RequestFactory().get("/")
    request.user = AnonymousUser()
    return render_to_string(
        "pages/teams/compare.html",
        {
            "my_team": SimpleNamespace(
                id=1, team_name="My XI", current_total_points=394
            ),
            "rival_team": SimpleNamespace(
                id=101, team_name="Rival XI", current_total_points=679
            ),
            "comparison": comparison,
        },
        request=request,
    )


def test_comparison_renders_fetched_entry_totals_instead_of_stale_profiles():
    comparison = comparison_from_entries(
        {"summary_overall_points": 704}, {"summary_overall_points": 690}
    )
    html = render_scoreboard(comparison)
    assert "704" in html and "690" in html
    assert "+14 pts ahead" in html
    assert "394" not in html and "285 pts behind" not in html
    assert "Season points / FPL entry totals" in html
    assert 'data-my-points="704"' in html
    assert 'data-rival-points="690"' in html
    assert 'js/comparison-share.js' in html


def test_zero_entry_total_does_not_fall_back_to_stored_points():
    comparison = comparison_from_entries(
        {"summary_overall_points": 0}, {"summary_overall_points": 12}
    )
    html = render_scoreboard(comparison)
    assert "12 pts behind" in html
    assert "394" not in html and "679" not in html
    assert 'data-my-points="0"' in html


@pytest.mark.parametrize(
    "my_entry,rival_entry",
    [
        (None, {"summary_overall_points": 690}),
        ({}, {"summary_overall_points": 690}),
        ({"summary_overall_points": 704}, None),
        (None, None),
    ],
)
def test_missing_entry_totals_do_not_invent_a_points_gap(my_entry, rival_entry):
    comparison = comparison_from_entries(my_entry, rival_entry)
    html = render_scoreboard(comparison)
    assert "Totals not available" in html
    assert "pts ahead" not in html and "pts behind" not in html
    assert "394" not in html and "679" not in html
    my_points = comparison["season_totals"]["mine"]
    assert f'data-my-points="{my_points if my_points is not None else ""}"' in html
