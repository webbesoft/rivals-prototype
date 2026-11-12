from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from time import perf_counter
from unittest.mock import patch

import pytest
from background_task.models import Task
from background_task.tasks import tasks
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from rivals.models import (
    GameweekData,
    MiniLeague,
    MiniLeagueTeam,
    SyncState,
    Team,
    TrackedTeam,
    Transfer,
    UserMiniLeague,
)
from rivals.services.fpl_api_service import FplApiService
from rivals.services.gameweek_context_service import GameweekContextService as Context
from rivals.services.refresh_service import enqueue_refresh, enqueue_user
from rivals.tasks import refresh_resource, refresh_tick
from rivals.views.dashboard_views import fetch_recent_activity

NOW = datetime(2026, 11, 10, 12, tzinfo=dt_timezone.utc)
EVENTS = [
    {"id": 1, "deadline_time": "2026-08-15T12:00:00Z", "finished": True},
    {
        "id": 10,
        "deadline_time": "2026-11-07T12:00:00Z",
        "finished": False,
        "is_current": True,
    },
    {"id": 11, "deadline_time": "2026-11-14T12:00:00Z", "finished": False},
]


@pytest.fixture(autouse=True)
def clean_cache():
    cache.clear()
    with patch("django.utils.timezone.now", return_value=NOW):
        yield
    cache.clear()


@pytest.fixture
def stored_context(db):
    return SyncState.objects.create(
        key="context",
        kind="context",
        status="ok",
        last_success_at=timezone.now(),
        payload={"events": EVENTS, "fixtures": [], "stamp": "2026/27:10"},
    )


@pytest.fixture
def manager(db):
    team = Team.objects.create(
        fpl_team_id="101", team_name="My team", current_season="2026/27"
    )
    user = get_user_model().objects.create_user(
        username="manager",
        email="manager@example.com",
        password="test123456",
        team=team,
    )
    rival = Team.objects.create(
        fpl_team_id="102", team_name="Rival", current_season="2026/27"
    )
    TrackedTeam.objects.create(user=user, team=rival)
    return user, rival


def test_deadline_rollover_ignores_lagging_is_current_flag():
    before = Context.derive(EVENTS, now=NOW)
    after = Context.derive(
        EVENTS, now=datetime(2026, 11, 14, 12, tzinfo=dt_timezone.utc)
    )
    assert before["scoring_gameweek"] == 10
    assert before["decision_gameweek"] == 11
    assert after["scoring_gameweek"] == 11
    assert after["decision_gameweek"] is None
    assert before["season"] == "2026/27"


def test_preseason_unknown_and_finished_season_are_distinct():
    assert Context.derive([], now=NOW)["scoring_gameweek"] is None
    preseason = Context.derive(EVENTS, now=datetime(2026, 8, 1, tzinfo=dt_timezone.utc))
    assert preseason["scoring_gameweek"] is None
    assert preseason["decision_gameweek"] == 1
    assert Context.derive(EVENTS, now=NOW + timedelta(days=30))["next_deadline"] is None


def test_freshness_policy_changes_for_matches_and_deadlines():
    assert Context.derive(EVENTS, now=NOW)["refresh_seconds"] == 1800
    assert (
        Context.derive(EVENTS, [{"started": True, "finished": False}], NOW)[
            "refresh_seconds"
        ]
        == 120
    )
    assert (
        Context.derive(EVENTS, now=datetime(2026, 11, 14, 10, tzinfo=dt_timezone.utc))[
            "refresh_seconds"
        ]
        == 120
    )


def test_api_failure_does_not_substitute_gw1():
    with patch.object(FplApiService, "fetch_bootstrap_static", return_value=None):
        assert FplApiService().fetch_current_gameweek() is None
        assert FplApiService().get_latest_passed_deadline() == (None, None)


@pytest.mark.django_db
def test_duplicate_enqueue_is_durable_and_cache_independent(stored_context):
    context = Context.cached()
    assert enqueue_refresh("players", context=context)
    cache.clear()
    assert not enqueue_refresh("players", force=True, context=context)
    assert Task.objects.filter(task_name="rivals.tasks.refresh_resource").count() == 1


@pytest.mark.django_db
def test_expired_lease_recovery_rejects_old_generation(stored_context):
    enqueue_refresh("players")
    state = SyncState.objects.get(key="players")
    old_generation = state.generation
    SyncState.objects.filter(pk=state.pk).update(
        lease_until=timezone.now() - timedelta(seconds=1)
    )
    assert enqueue_refresh("players")
    with patch("rivals.tasks.ingest") as ingest:
        refresh_resource.now("players", old_generation)
        ingest.assert_not_called()
    state.refresh_from_db()
    assert state.generation == old_generation + 1


@pytest.mark.django_db
def test_failure_preserves_last_good_snapshot_and_retries_with_backoff(stored_context):
    enqueue_refresh("players")
    state = SyncState.objects.get(key="players")
    previous = timezone.now() - timedelta(hours=1)
    state.last_success_at = previous
    state.payload = {"saved": "data"}
    state.save()
    with patch("rivals.tasks.ingest", side_effect=RuntimeError("upstream unavailable")):
        refresh_resource.now(state.key, state.generation)
    state.refresh_from_db()
    assert state.status == "error" and state.failures == 1
    assert state.last_success_at == previous
    assert state.payload == {"saved": "data"}
    assert state.next_attempt_at > timezone.now()
    assert not enqueue_refresh("players", force=True)
    SyncState.objects.filter(pk=state.pk).update(
        next_attempt_at=timezone.now() - timedelta(seconds=1)
    )
    assert enqueue_refresh("players")
    state.refresh_from_db()
    with patch("rivals.tasks.ingest", return_value={}):
        refresh_resource.now(state.key, state.generation)
    state.refresh_from_db()
    assert state.status == "ok" and state.failures == 0
    assert state.last_success_at > previous


@pytest.mark.django_db
def test_gameweek_rollover_bypasses_quiet_period(stored_context):
    SyncState.objects.create(
        key="players",
        kind="players",
        status="ok",
        last_success_at=timezone.now(),
        payload={"stamp": "2026/27:9"},
    )
    assert enqueue_refresh("players")


@pytest.mark.django_db
def test_transfer_only_worker_accepts_empty_list_without_marking_full_sync(
    manager, stored_context
):
    user, rival = manager
    enqueue_refresh("transfers", rival.pk)
    state = SyncState.objects.get(key=f"transfers:{rival.pk}")
    with patch("rivals.tasks.FplApiService") as factory:
        factory.return_value.fetch_team_transfers.return_value = []
        refresh_resource.now(state.key, state.generation)
    state.refresh_from_db()
    rival.refresh_from_db()
    assert state.status == "ok"
    assert state.last_success_at is not None
    assert rival.last_synced_at is None


@pytest.mark.django_db
def test_worker_history_does_not_fetch_picks(manager, stored_context):
    user, rival = manager
    enqueue_refresh("history", rival.pk)
    state = SyncState.objects.get(key=f"history:{rival.pk}")
    with patch("rivals.tasks.FplApiService") as factory:
        api = factory.return_value
        api.fetch_team_history.return_value = {
            "current": [
                {
                    "event": 10,
                    "points": 61,
                    "total_points": 601,
                    "event_transfers": 0,
                    "event_transfers_cost": 0,
                    "points_on_bench": 4,
                    "bank": 10,
                }
            ],
            "chips": [{"event": 10, "name": "freehit"}],
        }
        refresh_resource.now(state.key, state.generation)
        api.fetch_team_picks.assert_not_called()
    history = GameweekData.objects.get(team=rival, gameweek=10)
    assert history.points == 61 and history.bank == 10
    assert history.chip_name == "freehit" and history.chip_played
    assert SyncState.objects.get(pk=state.pk).status == "ok"


@pytest.mark.django_db
def test_outage_does_not_reset_team_on_season_transition(manager, stored_context):
    _, rival = manager
    rival.current_season = "2025/26"
    rival.current_total_points = 2000
    rival.save()
    enqueue_refresh("profile", rival.pk)
    state = SyncState.objects.get(key=f"profile:{rival.pk}")
    with patch("rivals.tasks.FplApiService") as factory:
        factory.return_value.fetch_team_basic_info.return_value = None
        refresh_resource.now(state.key, state.generation)
    rival.refresh_from_db()
    assert rival.current_season == "2025/26" and rival.current_total_points == 2000


@pytest.mark.django_db
def test_dispatcher_schedules_all_subscribed_resources_and_heartbeat(
    manager, stored_context
):
    user, rival = manager
    league = MiniLeague.objects.create(fpl_league_id="88", name="My league")
    UserMiniLeague.objects.create(user=user, mini_league=league)
    with patch(
        "requests.get", side_effect=AssertionError("Dispatcher must not call FPL")
    ):
        refresh_tick.now()
    assert SyncState.objects.get(key="worker").last_success_at
    for key in [
        "players",
        f"profile:{user.team_id}",
        f"history:{rival.pk}",
        f"transfers:{rival.pk}",
        f"league:{league.pk}",
    ]:
        assert SyncState.objects.get(key=key).status == "queued"


@pytest.mark.django_db
def test_real_task_runner_executes_reserved_job(stored_context):
    enqueue_refresh("players")
    with patch("rivals.tasks.ingest", return_value={}):
        assert tasks.run_next_task()
    assert SyncState.objects.get(key="players").status == "ok"
    assert Task.objects.filter(task_name="rivals.tasks.refresh_resource").count() == 0


@pytest.mark.django_db
def test_worker_startup_deduplicates_recurring_dispatcher():
    with patch("rivals.management.commands.fpl_worker.call_command") as process:
        call_command("fpl_worker", duration=1)
        call_command("fpl_worker", duration=1)
    assert Task.objects.filter(task_name="rivals.tasks.refresh_tick").count() == 1
    process.assert_called_with("process_tasks", duration=1, sleep=2)


@pytest.mark.django_db
def test_independent_intel_pages_filters_and_access(manager, stored_context):
    user, rival = manager
    for gw in range(1, 26):
        Transfer.objects.create(
            team=rival,
            gameweek=gw,
            season="2026/27",
            player_in_name=f"In {gw}",
            player_out_name="Out",
        )
        GameweekData.objects.create(
            team=rival, gameweek=gw, season="2026/27", points=gw
        )
    Transfer.objects.create(
        team=rival,
        gameweek=38,
        season="2025/26",
        player_in_name="Old",
        player_out_name="Old",
    )
    outsider = Team.objects.create(fpl_team_id="999", team_name="Untracked")
    Transfer.objects.create(
        team=outsider,
        gameweek=10,
        season="2026/27",
        player_in_name="Secret",
        player_out_name="Out",
    )
    result = fetch_recent_activity(user, {"transfers_page": "2", "scores_page": "3"})
    assert result["transfers"].number == 2 and len(result["transfers"]) == 10
    assert result["gameweek_data"].number == 3 and len(result["gameweek_data"]) == 5
    assert result["transfers"].paginator.count == 25
    assert (
        fetch_recent_activity(user, {"season": "2025/26"})["transfers"].paginator.count
        == 1
    )
    assert (
        fetch_recent_activity(user, {"gameweek": "10"})["transfers"].paginator.count
        == 1
    )
    assert (
        fetch_recent_activity(user, {"season": "all"})["transfers"].paginator.count
        == 26
    )
    client = Client()
    client.force_login(user)
    response = client.get(
        reverse("rivals:intel"),
        {"transfers_page": "2", "scores_page": "3", "season": "2026/27", "page": "2"},
    )
    assert response.status_code == 200
    html = response.content.decode()
    assert "scores_page=3" in html and "page=2" in html
    assert "season=2026%2F27" in html
    assert "Page 2 of 3" in html and "Page 3 of 3" in html
    assert "Secret" not in html


@pytest.mark.django_db
def test_initial_pages_and_polling_make_zero_upstream_requests(manager, stored_context):
    user, rival = manager
    league = MiniLeague.objects.create(fpl_league_id="88", name="My league")
    UserMiniLeague.objects.create(user=user, mini_league=league)
    client = Client()
    client.force_login(user)
    with patch(
        "requests.get", side_effect=AssertionError("Initial pages cannot call FPL")
    ) as http:
        start = perf_counter()
        dashboard = client.get(reverse("rivals:dashboard"))
        elapsed = perf_counter() - start
        assert dashboard.status_code == 200
        assert (
            client.get(reverse("rivals:teams.show", args=[user.team_id])).status_code
            == 200
        )
        assert (
            client.get(reverse("rivals:minileagues.show", args=[league.pk])).status_code
            == 200
        )
        assert client.get(reverse("rivals:refresh-status")).status_code == 200
        assert client.get(reverse("rivals:intel")).status_code == 200
        assert http.call_count == 0
    print(
        f"Dashboard local request: {elapsed * 1000:.1f} ms; upstream calls: {http.call_count}"
    )


@pytest.mark.django_db
def test_refresh_controls_require_post_and_team_authorization(manager, stored_context):
    user, rival = manager
    outsider = Team.objects.create(fpl_team_id="999", team_name="Untracked")
    client = Client()
    client.force_login(user)
    assert client.get(reverse("rivals:rivals.sync")).status_code == 405
    assert client.get(reverse("rivals:teams.sync", args=[rival.pk])).status_code == 405
    response = client.post(reverse("rivals:teams.sync", args=[outsider.pk]))
    assert response.status_code == 302
    assert response.url == reverse("rivals:teams.show", args=[outsider.pk])
    assert not SyncState.objects.filter(object_id=outsider.pk).exists()
    with patch(
        "requests.get", side_effect=AssertionError("Refresh controls must queue")
    ):
        assert (
            client.post(reverse("rivals:teams.sync", args=[rival.pk])).status_code
            == 302
        )
    assert SyncState.objects.get(key=f"transfers:{rival.pk}").status == "queued"


@pytest.mark.django_db
def test_transfer_batches_keep_same_timestamp_pairs_and_remain_idempotent(
    manager, stored_context
):
    from rivals.services.team_sync_service import TeamSyncService

    _, rival = manager
    batch = [
        {
            "event": 10,
            "time": "2026-11-07T10:00:00Z",
            "element_in": 1,
            "element_out": 2,
        },
        {
            "event": 10,
            "time": "2026-11-07T10:00:00Z",
            "element_in": 3,
            "element_out": 4,
        },
    ]
    service = TeamSyncService(rival)
    assert service.sync_transfers(transfers=batch, sync_missing_players=False)
    assert service.sync_transfers(transfers=batch, sync_missing_players=False)
    assert rival.transfers.count() == 2


@pytest.mark.django_db
def test_failed_profile_import_rolls_back_season_reset(manager, stored_context):
    _, rival = manager
    rival.current_season = "2025/26"
    rival.current_total_points = 2000
    rival.save()
    enqueue_refresh("profile", rival.pk)
    state = SyncState.objects.get(key=f"profile:{rival.pk}")
    with patch("rivals.tasks.FplApiService") as factory:
        factory.return_value.fetch_team_basic_info.return_value = {
            "name": "Incomplete response"
        }
        refresh_resource.now(state.key, state.generation)
    rival.refresh_from_db()
    assert rival.current_season == "2025/26" and rival.current_total_points == 2000
    assert SyncState.objects.get(pk=state.pk).status == "error"


@pytest.mark.django_db
def test_worker_health_command_fails_without_heartbeat_or_with_overdue_jobs():
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command("check_fpl_worker")
    SyncState.objects.create(
        key="worker", kind="worker", last_success_at=timezone.now()
    )
    call_command("check_fpl_worker")
    SyncState.objects.create(
        key="players",
        kind="players",
        status="running",
        lease_until=timezone.now() - timedelta(minutes=1),
    )
    with pytest.raises(CommandError):
        call_command("check_fpl_worker")


@pytest.mark.django_db
def test_restart_does_not_duplicate_a_locked_dispatcher():
    with patch("rivals.management.commands.fpl_worker.call_command"):
        call_command("fpl_worker", duration=1)
        Task.objects.filter(task_name="rivals.tasks.refresh_tick").update(
            locked_by="123", locked_at=timezone.now()
        )
        call_command("fpl_worker", duration=1)
    assert Task.objects.filter(task_name="rivals.tasks.refresh_tick").count() == 1


@pytest.mark.django_db
def test_incomplete_league_import_retains_standings_and_last_success(
    stored_context, caplog
):
    from rivals.models import MiniLeagueTeam

    league = MiniLeague.objects.create(fpl_league_id="88", name="Saved league")
    team = Team.objects.create(fpl_team_id="101", team_name="Saved team")
    MiniLeagueTeam.objects.create(
        mini_league=league,
        team=team,
        current_rank=1,
        total_points=500,
        season="2026/27",
    )
    enqueue_refresh("league", league.pk)
    state = SyncState.objects.get(key=f"league:{league.pk}")
    with patch("rivals.tasks.FplApiService") as factory:
        factory.return_value.fetch_mini_league_page.return_value = None
        refresh_resource.now(state.key, state.generation)
    league.refresh_from_db()
    assert league.name == "Saved league"
    assert league.last_synced_at is None
    assert league.mini_league_teams.get().total_points == 500
    assert SyncState.objects.get(pk=state.pk).status == "error"
    assert "FPL league 88" in caplog.text
    assert "Standings page 1 unavailable" in caplog.text


@pytest.mark.parametrize("failure", ["limit", "unavailable", "complete"])
def test_league_pagination_reports_why_standings_are_incomplete(failure):
    first = {
        "league": {"name": "League"},
        "standings": {
            "results": [{"entry": 1}],
            "has_next": True,
        },
    }
    second = {"standings": {"results": [{"entry": 2}], "has_next": False}}
    api = FplApiService()
    with patch.object(
        api,
        "make_request",
        side_effect=[first, None if failure == "unavailable" else second],
    ) as request:
        data = api.fetch_mini_league("88", max_pages=1 if failure == "limit" else 2)
    assert data["is_complete"] == (failure == "complete")
    assert request.call_count == (1 if failure == "limit" else 2)
    if failure == "limit":
        assert (
            data["incomplete_reason"]
            == "Standings exceed the 1-page import limit (1 entries fetched)"
        )
    elif failure == "unavailable":
        assert data["incomplete_reason"] == "Standings page 2 unavailable"
    else:
        assert data["incomplete_reason"] is None
        assert data["standings"] == [{"entry": 1}, {"entry": 2}]


@pytest.mark.django_db
def test_api_sync_acceptance_and_access_controls(manager, stored_context):
    user, rival = manager
    user.is_verified = True
    user.save(update_fields=["is_verified"])
    league = MiniLeague.objects.create(fpl_league_id="88", name="My league")
    subscription = UserMiniLeague.objects.create(user=user, mini_league=league)
    client = Client()
    client.force_login(user)
    with patch(
        "requests.get", side_effect=AssertionError("API sync must only enqueue")
    ):
        assert client.post(reverse("api:team_sync", args=[rival.pk])).status_code == 202
        assert (
            client.post(reverse("api:league_sync", args=[league.pk])).status_code == 202
        )
    subscription.active = False
    subscription.save()
    assert client.post(reverse("api:league_sync", args=[league.pk])).status_code == 403
    user.tracked_teams.filter(team=rival).update(active=False)
    assert client.post(reverse("api:team_sync", args=[rival.pk])).status_code == 403


@pytest.mark.django_db
def test_cold_dashboard_and_unknown_context_do_not_fetch_fpl(manager):
    user, _ = manager
    client = Client()
    client.force_login(user)
    with patch("requests.get", side_effect=AssertionError("No synchronous FPL reads")):
        response = client.get(reverse("rivals:dashboard"))
    assert response.status_code == 200
    assert "Figures may be out of date" in response.content.decode()
    assert SyncState.objects.get(key="context").status == "queued"


@pytest.mark.django_db
def test_warm_dashboard_checks_refresh_freshness_in_one_batch(manager, stored_context):
    user, _ = manager
    Team.objects.bulk_create(
        [
            Team(fpl_team_id=str(1000 + index), team_name=f"Rival {index}")
            for index in range(30)
        ]
    )
    TrackedTeam.objects.bulk_create(
        [
            TrackedTeam(user=user, team=team)
            for team in Team.objects.filter(
                fpl_team_id__in=[str(1000 + i) for i in range(30)]
            )
        ]
    )
    enqueue_user(user)
    with CaptureQueriesContext(connection) as queries:
        enqueue_user(user)
    # Context, tracked IDs, league IDs, then all existing refresh states.
    assert len(queries) == 4
    assert not any(
        sql["sql"].startswith(("INSERT", "UPDATE", "BEGIN")) for sql in queries
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "league_count,page,visible_count", [(1, 1, 1), (10, 1, 10), (12, 2, 2)]
)
def test_dashboard_batches_projection_queries(
    manager, stored_context, league_count, page, visible_count
):
    user, rival = manager
    for index in range(league_count):
        league = MiniLeague.objects.create(
            fpl_league_id=str(1000 + index), name=f"League {index:02d}"
        )
        UserMiniLeague.objects.create(user=user, mini_league=league)
        MiniLeagueTeam.objects.create(
            mini_league=league, team=user.team, current_rank=2, total_points=400
        )
        MiniLeagueTeam.objects.create(
            mini_league=league, team=rival, current_rank=1, total_points=500
        )
    client = Client()
    client.force_login(user)
    # Isolate projection reads from refresh scheduling writes. Exercise both a
    # cold projection cache and the next page load, with real template rendering.
    for _ in range(2):
        with (
            patch("rivals.views.dashboard_views.enqueue_user"),
            CaptureQueriesContext(connection) as queries,
        ):
            response = client.get(reverse("rivals:dashboard"), {"page": page})
        assert response.status_code == 200
        projections = response.context["mini_league_projections"]
        assert len(projections) == visible_count
        assert all(projection["current_rank"] == 2 for projection in projections)
        assert all(projection["projected_rank"] == 2 for projection in projections)
        assert all(projection["total_teams"] == 2 for projection in projections)
        standings_queries = [
            query["sql"]
            for query in queries
            if 'FROM "rivals_minileagueteam"' in query["sql"]
        ]
        # Each visible league reads one manager row, aggregate counts, and at
        # most three scores. No request hydrates the entire league's teams.
        assert len(standings_queries) == 3 * visible_count
        assert all(
            "COUNT(" in sql or "LIMIT 1" in sql or "LIMIT 3" in sql
            for sql in standings_queries
        )
        # Dashboard context and refresh status each read it once; the team and
        # league projections must reuse the dashboard's context.
        context_queries = [
            query["sql"]
            for query in queries
            if 'FROM "rivals_syncstate"' in query["sql"]
            and "\"key\" = 'context'" in query["sql"]
        ]
        assert len(context_queries) == 2
        assert len(response.context["mini_leagues"]) == visible_count


@pytest.mark.django_db
def test_real_context_job_persists_deadlines_for_other_processes():
    from unittest.mock import Mock

    bootstrap = Mock()
    bootstrap.json.return_value = {"events": EVENTS}
    fixtures = Mock()
    fixtures.json.return_value = []
    enqueue_refresh("context")
    with patch("requests.get", side_effect=[bootstrap, fixtures]) as http:
        assert tasks.run_next_task()
    assert http.call_count == 2
    assert all(call.kwargs["timeout"] == 5 for call in http.call_args_list)
    cache.clear()
    context = Context.cached()
    assert context["scoring_gameweek"] == 10
    assert context["decision_gameweek"] == 11
    assert not context["stale"]
    assert SyncState.objects.get(key="context").status == "ok"
