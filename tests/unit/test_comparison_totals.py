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
    assert "js/comparison-share.js" in html


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
def test_missing_entry_totals_fall_back_independently_to_stored_points(
    my_entry, rival_entry
):
    comparison = comparison_from_entries(my_entry, rival_entry)
    html = render_scoreboard(comparison)
    mine = comparison["season_totals"]["mine"]
    rival = comparison["season_totals"]["rival"]
    assert f'data-my-points="{394 if mine is None else mine}"' in html
    assert f'data-rival-points="{679 if rival is None else rival}"' in html
    assert "Latest stored total" in html
    assert "Totals not available" not in html


def test_captain_projection_uses_published_captain_only():
    service = TeamAnalysisService(api=Mock())
    service.get_captain_suggestions = Mock(
        return_value=[
            {
                "name": "Published captain",
                "team": "FC",
                "expected_points_next_gw": 4.5,
                "form": 4,
            }
        ]
    )
    captain = {"element": 1, "is_captain": True}
    result = service.get_captain_projection(
        [captain, {"element": 2, "is_captain": False}]
    )
    service.get_captain_suggestions.assert_called_once_with([captain], limit=1)
    assert result["expected_points"] == 9


def test_missing_and_zero_momentum_render_differently_in_page_and_share():
    for average, rendered in [(None, "—"), (0.0, "0.0")]:
        html = render_scoreboard(
            {"momentum": {"mine": {"average": average}, "rival": {"average": average}}}
        )
        assert f'data-my-form="{rendered}"' in html
        assert f"{rendered} <small>pts/GW</small>" in html


def test_momentum_missing_history_is_distinct_from_zero_points():
    service = TeamAnalysisService(api=Mock())
    # MagicMock supplies queryset slicing while retaining the result contract.
    from unittest.mock import MagicMock

    history = MagicMock()
    rows = history.filter.return_value.order_by.return_value.__getitem__.return_value
    rows.values_list.return_value = []
    team = SimpleNamespace(gameweek_data=history, current_season="2026/27")
    assert service.calculate_momentum(team)["average"] is None
    rows.values_list.return_value = [0, 0]
    assert service.calculate_momentum(team)["average"] == 0.0
