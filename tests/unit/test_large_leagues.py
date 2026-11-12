from importlib import import_module
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from background_task.models import Task
from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from rivals.models import (
    LeagueImport,
    LeagueImportPage,
    LeagueRankSnapshot,
    LeagueStandingsPage,
    MiniLeague,
    MiniLeagueTeam,
    SyncState,
    Team,
    TrackedTeam,
    UserMiniLeague,
)
from rivals.services.league_summary_service import (
    large_league_context,
    save_page,
    save_profile_ranks,
)
from rivals.services.refresh_service import enqueue_refresh
from rivals.tasks import refresh_resource

pytestmark = pytest.mark.django_db
CONTEXT = {
    "season": "2026/27",
    "scoring_gameweek": 10,
    "stale": False,
    "refresh_seconds": 1800,
}


@pytest.fixture(autouse=True)
def isolated_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def manager():
    team = Team.objects.create(
        fpl_team_id="123", team_name="My XI", current_season="2026/27"
    )
    user = get_user_model().objects.create_user(
        username="summary-owner",
        email="summary@example.com",
        team=team,
        is_verified=True,
    )
    league = MiniLeague.objects.create(
        fpl_league_id="88", name="Huge league", rank_count=40000
    )
    UserMiniLeague.objects.create(user=user, mini_league=league, source="fpl")
    return user, league


def page(number=1, entry=None, has_next=True):
    return {
        "league": {"name": "Huge league"},
        "standings": {
            "page": number,
            "has_next": has_next,
            "results": [
                {
                    "entry": entry or number,
                    "entry_name": f"Page {number} team",
                    "player_name": "Manager",
                    "rank": (number - 1) * 50 + 1,
                    "total": 600,
                }
            ],
        },
    }


def profile(user, rank=7500, previous=8000, gw=10):
    return {
        "id": int(user.team.fpl_team_id),
        "name": "My XI",
        "player_first_name": "A",
        "player_last_name": "Manager",
        "summary_overall_points": 550,
        "summary_event_points": 55,
        "last_deadline_total_transfers": 10,
        "summary_overall_rank": 123456,
        "current_event": gw,
        "leagues": {
            "classic": [
                {
                    "id": 88,
                    "name": "Huge league",
                    "league_type": "x",
                    "scoring": "c",
                    "entry_rank": rank,
                    "entry_last_rank": previous,
                    "rank_count": 40000,
                }
            ]
        },
    }


def run_resource(kind, pk, api):
    enqueue_refresh(kind, pk, context=CONTEXT)
    state = SyncState.objects.get(key=f"{kind}:{pk}")
    with (
        patch("rivals.tasks.FplApiService", return_value=api),
        patch("rivals.tasks.GameweekContextService.cached", return_value=CONTEXT),
    ):
        refresh_resource.now(state.key, state.generation)
    state.refresh_from_db()
    return state


def test_profile_snapshots_are_season_and_gameweek_scoped_and_idempotent(manager):
    user, league = manager
    save_profile_ranks(user.team, profile(user), CONTEXT)
    save_profile_ranks(user.team, profile(user, rank=7400), CONTEXT)
    assert LeagueRankSnapshot.objects.count() == 1
    snapshot = LeagueRankSnapshot.objects.get()
    assert snapshot.current_rank == 7400 and snapshot.rank_change == 600
    save_profile_ranks(
        user.team, profile(user, rank=7300, gw=11), {**CONTEXT, "scoring_gameweek": 11}
    )
    save_profile_ranks(
        user.team, profile(user, rank=100, previous=0), {**CONTEXT, "season": "2027/28"}
    )
    assert LeagueRankSnapshot.objects.count() == 3
    assert LeagueRankSnapshot.objects.get(season="2027/28").rank_change is None
    # An upstream profile lagging the new deadline must not overwrite history.
    save_profile_ranks(
        user.team, profile(user, rank=1, gw=10), {**CONTEXT, "scoring_gameweek": 11}
    )
    assert (
        LeagueRankSnapshot.objects.get(season="2026/27", gameweek=10).current_rank
        == 7400
    )
    for invalid in (None, 0, -1, True, "100"):
        save_profile_ranks(user.team, profile(user, rank=invalid), CONTEXT)
    assert (
        LeagueRankSnapshot.objects.get(season="2026/27", gameweek=10).current_rank
        == 7400
    )


def test_actual_profile_worker_persists_published_ranks(manager):
    user, league = manager
    api = Mock()
    api.fetch_team_basic_info.return_value = profile(user)
    state = run_resource("profile", user.team_id, api)
    assert state.status == "ok"
    assert (
        LeagueRankSnapshot.objects.get(team=user.team, mini_league=league).current_rank
        == 7500
    )
    api.fetch_mini_league_page.assert_not_called()


def test_large_league_refresh_only_fetches_leaders_and_preserves_old_full_import(
    manager,
):
    user, league = manager
    MiniLeagueTeam.objects.create(
        mini_league=league, team=user.team, current_rank=9999, total_points=400
    )
    run = LeagueImport.objects.create(league=league, stamp="2026/27:10", next_page=200)
    LeagueImportPage.objects.create(
        import_run=run, number=1, standings=page()["standings"]["results"]
    )
    api = Mock()
    api.fetch_mini_league_page.return_value = page()
    state = run_resource("league", league.pk, api)
    assert state.status == "ok"
    api.fetch_mini_league_page.assert_called_once_with("88", 1)
    assert not LeagueImport.objects.exists()
    assert league.mini_league_teams.get().current_rank == 9999
    assert LeagueStandingsPage.objects.get().rows[0]["entry"] == 1
    assert Team.objects.count() == 1
    assert not Task.objects.filter(task_params__contains=f'"{state.key}", 2').exists()


def test_unknown_size_stops_at_configured_limit_without_publishing_partial_rows(
    manager, settings
):
    user, league = manager
    settings.LEAGUE_FULL_IMPORT_LIMIT = 2
    league.rank_count = None
    league.save()
    api = Mock()
    api.fetch_mini_league_page.side_effect = [page(1), page(2)]
    state = run_resource("league", league.pk, api)
    league.refresh_from_db()
    assert state.status == "ok" and league.summary_only
    assert api.fetch_mini_league_page.call_count == 2
    assert not MiniLeagueTeam.objects.exists()
    assert not LeagueImport.objects.exists()
    assert LeagueStandingsPage.objects.get().number == 1


def test_old_oversized_checkpoint_switches_without_fetching_more_pages(
    manager, settings
):
    user, league = manager
    settings.LEAGUE_FULL_IMPORT_LIMIT = 2
    league.rank_count = None
    league.save()
    run = LeagueImport.objects.create(
        league=league,
        stamp="2026/27:10",
        next_page=4,
        league_info={"name": league.name},
    )
    for number in range(1, 4):
        LeagueImportPage.objects.create(
            import_run=run,
            number=number,
            standings=page(number)["standings"]["results"],
        )
    api = Mock()
    state = run_resource("league", league.pk, api)
    assert state.status == "ok"
    api.fetch_mini_league_page.assert_not_called()
    league.refresh_from_db()
    assert league.summary_only


def test_failed_refresh_keeps_cached_page_and_respects_backoff(manager):
    user, league = manager
    cached = save_page(league, CONTEXT["season"], 150, page(150))
    api = Mock()
    api.fetch_mini_league_page.return_value = None
    state = run_resource("league_page", cached.pk, api)
    assert state.status == "error"
    cached.refresh_from_db()
    assert cached.rows[0]["entry"] == 150
    assert not enqueue_refresh("league_page", cached.pk, force=True, context=CONTEXT)


def test_large_league_view_is_bounded_and_never_uses_retained_standings(manager):
    user, league = manager
    save_profile_ranks(user.team, profile(user), CONTEXT)
    MiniLeagueTeam.objects.create(
        mini_league=league, team=user.team, current_rank=1, total_points=99999
    )
    client = Client()
    client.force_login(user)
    url = reverse("rivals:minileagues.show", args=[league.pk])
    with (
        patch(
            "rivals.views.minileague_views.GameweekContextService.cached",
            return_value=CONTEXT,
        ),
        patch("requests.get", side_effect=AssertionError("No HTTP in views")),
        patch("rivals.views.minileague_views.ProjectionService") as projections,
        CaptureQueriesContext(connection) as queries,
    ):
        response = client.get(url)
    assert response.status_code == 200
    assert response.context["rank_snapshot"].current_rank == 7500
    assert set(LeagueStandingsPage.objects.values_list("number", flat=True)) == {
        1,
        149,
        150,
        151,
    }
    assert not response.context["found_user"]
    assert "Projected Finish" not in response.content.decode()
    assert "99999" not in response.content.decode()
    assert not any('FROM "rivals_minileagueteam"' in item["sql"] for item in queries)
    projections.assert_not_called()
    task_count = Task.objects.count()
    with patch(
        "rivals.views.minileague_views.GameweekContextService.cached",
        return_value=CONTEXT,
    ):
        client.get(url)
    assert Task.objects.count() == task_count


def test_cached_pages_verify_entry_and_deduplicate_tied_boundaries(manager):
    user, league = manager
    save_profile_ranks(user.team, profile(user, rank=75), CONTEXT)
    first = save_page(league, CONTEXT["season"], 1, page())
    first.page_size = 50
    first.save()
    save_page(league, CONTEXT["season"], 2, page(2, entry=123))
    save_page(league, CONTEXT["season"], 3, page(3, entry=123))
    with patch("requests.get", side_effect=AssertionError("No HTTP")):
        result = large_league_context(user, league, CONTEXT, {})
    assert result["found_user"]
    assert len([row for row in result["visible_rows"] if row["entry"] == 123]) == 1
    assert not result["waiting"]
    # A tied-rank page estimate that misses the entry never triggers a crawl.
    LeagueStandingsPage.objects.exclude(number=1).update(rows=[])
    result = large_league_context(user, league, CONTEXT, {})
    assert not result["found_user"]
    assert LeagueStandingsPage.objects.count() == 3


def test_dashboard_displays_published_rank_and_no_large_league_projection(manager):
    user, league = manager
    save_profile_ranks(user.team, profile(user), CONTEXT)
    client = Client()
    client.force_login(user)
    with (
        patch("rivals.views.dashboard_views.enqueue_user"),
        patch(
            "rivals.views.dashboard_views.GameweekContextService.cached",
            return_value=CONTEXT,
        ),
        patch(
            "rivals.services.projection_service.ProjectionService.get_team_mini_league_summary"
        ) as summary,
    ):
        response = client.get(reverse("rivals:dashboard"))
    assert response.status_code == 200
    summary.assert_not_called()
    membership = list(response.context["mini_leagues"])[0]
    assert membership.total_teams == 40000
    assert membership.rank_snapshot.current_rank == 7500
    assert "▲ +500" in response.content.decode()


def test_migration_marks_previously_imported_large_leagues_without_deleting_history(
    manager, settings
):
    user, league = manager
    settings.LEAGUE_FULL_IMPORT_LIMIT = 1
    league.rank_count = None
    league.save()
    other = Team.objects.create(fpl_team_id="456", team_name="Other")
    for team in (user.team, other):
        MiniLeagueTeam.objects.create(
            mini_league=league, team=team, current_rank=1, total_points=100
        )
    import_module("rivals.migrations.0020_league_summaries").mark_large_leagues(
        apps, SimpleNamespace(connection=connection)
    )
    league.refresh_from_db()
    assert league.summary_only
    assert league.mini_league_teams.count() == 2


def test_summary_api_returns_bounded_partial_results_and_checks_access(manager):
    user, league = manager
    save_profile_ranks(user.team, profile(user), CONTEXT)
    client = Client()
    client.force_login(user)
    url = reverse("api:league_detail", args=[league.pk])
    with (
        patch(
            "rivals.services.gameweek_context_service.GameweekContextService.cached",
            return_value=CONTEXT,
        ),
        patch("requests.get", side_effect=AssertionError("No HTTP")),
        CaptureQueriesContext(connection) as queries,
    ):
        response = client.get(url, {"section": "table", "standings_page": "2"})
    assert response.status_code == 200
    data = response.json()
    assert data["partial"] and data["summary_only"]
    assert data["user_rank"]["current_rank"] == 7500
    assert not any('FROM "rivals_minileagueteam"' in item["sql"] for item in queries)
    assert set(LeagueStandingsPage.objects.values_list("number", flat=True)) == {1, 2}
    user.customminileagues.update(active=False)
    assert client.get(url).status_code == 403


def test_tracking_from_cached_page_only_creates_the_selected_team(manager):
    user, league = manager
    save_page(league, CONTEXT["season"], 150, page(150, entry=456))
    client = Client()
    client.force_login(user)
    url = reverse("rivals:minileagues.track", args=[league.pk, 150, 456])
    with (
        patch(
            "rivals.views.minileague_views.GameweekContextService.cached",
            return_value=CONTEXT,
        ),
        patch("requests.get", side_effect=AssertionError("Tracking only queues")),
    ):
        assert client.get(url).status_code == 405
        assert client.post(url).status_code == 302
    team = Team.objects.get(fpl_team_id="456")
    assert TrackedTeam.objects.filter(user=user, team=team, active=True).exists()
    assert SyncState.objects.get(key=f"profile:{team.pk}").status == "queued"
    assert Team.objects.count() == 2
    url = reverse("rivals:minileagues.track", args=[league.pk, 150, 789])
    with patch(
        "rivals.views.minileague_views.GameweekContextService.cached",
        return_value=CONTEXT,
    ):
        assert client.post(url).status_code == 404
    assert not Team.objects.filter(fpl_team_id="789").exists()
    user.customminileagues.update(active=False)
    assert (
        client.post(
            reverse("rivals:minileagues.track", args=[league.pk, 150, 456])
        ).status_code
        == 404
    )


def test_tracking_limit_rolls_back_new_team(manager):
    user, league = manager
    teams = Team.objects.bulk_create(
        [Team(fpl_team_id=str(1000 + i), team_name="Tracked") for i in range(10)]
    )
    TrackedTeam.objects.bulk_create(
        [TrackedTeam(user=user, team=team) for team in teams]
    )
    save_page(league, CONTEXT["season"], 2, page(2, entry=456))
    client = Client()
    client.force_login(user)
    with patch(
        "rivals.views.minileague_views.GameweekContextService.cached",
        return_value=CONTEXT,
    ):
        assert (
            client.post(
                reverse("rivals:minileagues.track", args=[league.pk, 2, 456])
            ).status_code
            == 302
        )
    assert not Team.objects.filter(fpl_team_id="456").exists()


def test_leader_gaps_only_compare_matching_season_totals(manager):
    user, league = manager
    save_profile_ranks(user.team, profile(user), CONTEXT)
    save_page(league, CONTEXT["season"], 1, page())
    league.start_event = 1
    result = large_league_context(user, league, CONTEXT, {"section": "table"})
    assert result["gap_to_leader"] == 50 and result["gap_to_podium"] == 50
    league.start_event = 5
    result = large_league_context(user, league, CONTEXT, {"section": "table"})
    assert result["gap_to_leader"] is None and not result["can_compare_points"]
