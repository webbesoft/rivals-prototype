from unittest.mock import Mock, patch

import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from rivals.models import Team
from rivals.services.score_summary_service import ScoreSummaryService


@pytest.fixture(autouse=True)
def isolated_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def api():
    with patch("rivals.services.score_summary_service.FplApiService") as factory:
        client = factory.return_value
        client.fetch_bootstrap_static.return_value = {
            "events": [
                {"id": 9, "average_entry_score": 40, "finished": True},
                {"id": 10, "average_entry_score": 35, "finished": False},
            ]
        }
        client.fetch_team_basic_info.return_value = {
            "current_event": 10,
            "summary_event_points": 62,
            "summary_overall_points": 600,
            "summary_overall_rank": 12000,
        }
        yield client


def service():
    return ScoreSummaryService(Mock(fpl_team_id="12345"))


def test_scores_match_entry_gameweek_and_share_fresh_snapshot(api):
    summary = service().refresh()
    assert (summary["gameweek"], summary["points"], summary["average"]) == (10, 62, 35)
    assert summary["difference"] == 27
    assert service().refresh() == summary
    api.fetch_team_basic_info.assert_called_once_with("12345")
    api.fetch_team_history.assert_not_called()
    api.fetch_team_picks.assert_not_called()


def test_zero_scores_and_average_are_valid(api):
    api.fetch_team_basic_info.return_value["summary_event_points"] = 0
    api.fetch_bootstrap_static.return_value["events"][1]["average_entry_score"] = 0
    summary = service().refresh()
    assert summary["points"] == summary["average"] == summary["difference"] == 0


def test_rollover_uses_entry_event_instead_of_current_bootstrap_flag(api):
    api.fetch_bootstrap_static.return_value["events"][1]["is_current"] = True
    api.fetch_team_basic_info.return_value["current_event"] = 9
    summary = service().refresh()
    assert summary["gameweek"] == 9
    assert summary["average"] == 40


def test_failed_refresh_retains_stale_scores_and_throttles_retries(api):
    summary = service().refresh()
    summary["updated_at"] -= timezone.timedelta(minutes=10)
    cache.set(service().key, summary)
    cache.delete(f"{service().key}_attempt")
    api.fetch_team_basic_info.return_value = None
    stale = service().refresh()
    assert stale["stale"] and stale["unavailable"]
    assert stale["points"] == 62
    service().refresh()
    assert api.fetch_team_basic_info.call_count == 2


def test_missing_bootstrap_does_not_invent_a_gameweek(api):
    api.fetch_bootstrap_static.return_value = None
    summary = service().refresh()
    assert summary["unavailable"]
    assert "gameweek" not in summary
    api.fetch_team_basic_info.assert_not_called()


def test_preseason_has_no_score_or_average(api):
    api.fetch_team_basic_info.return_value["current_event"] = None
    summary = service().refresh()
    assert summary["gameweek"] is None
    assert summary["points"] is None
    assert summary["average"] is None


def test_unknown_event_does_not_pair_score_with_wrong_average(api):
    api.fetch_team_basic_info.return_value["current_event"] = 11
    assert service().refresh()["unavailable"]
    assert service().cached() is None


@pytest.mark.django_db
def test_endpoint_requires_login_and_refresh_does_not_mark_full_sync(api):
    team = Team.objects.create(fpl_team_id="12345", team_name="Test FC")
    url = reverse("rivals:teams.score", args=[team.pk])
    client = Client()
    assert client.get(url).status_code == 302
    user = User.objects.create_user(
        username="score@example.com", email="score@example.com", password="test12345"
    )
    client.force_login(user)
    response = client.get(url)
    assert response.status_code == 200
    assert "Gameweek 10" in response.content.decode()
    assert "62" in response.content.decode()
    assert "35" in response.content.decode()
    assert "no-store" in response.headers["Cache-Control"]
    team.refresh_from_db()
    assert team.last_synced_at is None
    assert team.current_total_points == 0


@pytest.mark.django_db
def test_zero_scores_render_and_missing_team_is_404(api):
    team = Team.objects.create(fpl_team_id="12345", team_name="Test FC")
    api.fetch_team_basic_info.return_value["summary_event_points"] = 0
    user = User.objects.create_user(
        username="score@example.com", email="score@example.com", password="test12345"
    )
    client = Client()
    client.force_login(user)
    response = client.get(reverse("rivals:teams.score", args=[team.pk]))
    assert ">0</p>" in response.content.decode()
    assert (
        client.get(reverse("rivals:teams.score", args=[team.pk + 1])).status_code == 404
    )


@pytest.mark.django_db
@pytest.mark.parametrize("page", ["rivals:dashboard", "rivals:teams.show"])
def test_pages_show_score_panel_before_forecasts_without_score_network_calls(api, page):
    team = Team.objects.create(fpl_team_id="12345", team_name="Test FC")
    user = User.objects.create_user(
        username="score@example.com",
        email="score@example.com",
        password="test12345",
        team=team,
    )
    client = Client()
    client.force_login(user)
    url = reverse(page, args=[team.pk] if page.endswith("show") else [])
    with (
        patch(
            "rivals.services.projection_service.ProjectionService.project_team_finish",
            return_value={"completed_gameweeks": 10},
        ),
        patch(
            "rivals.services.team_analysis_service.TeamAnalysisService.get_cached_analysis",
            return_value=None,
        ),
        patch(
            "rivals.services.fpl_api_service.FplApiService.fetch_current_season",
            return_value=None,
        ),
    ):
        response = client.get(url)
    assert response.status_code == 200
    html = response.content.decode()
    assert 'aria-label="Gameweek score and average"' in html
    assert 'hx-trigger="load, every 120s"' in html
    assert reverse("rivals:teams.score", args=[team.pk]) in html
    assert "vs average" not in html
    assert html.index("Gameweek points") < html.index("Projected")
    api.fetch_team_basic_info.assert_not_called()
