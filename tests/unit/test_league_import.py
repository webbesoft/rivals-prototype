from unittest.mock import Mock, patch

import pytest
from django.utils import timezone

from rivals.models import LeagueImport, MiniLeague, MiniLeagueTeam, SyncState, Team
from rivals.services.league_import_service import LeagueImportService
from rivals.services.refresh_service import enqueue_refresh
from rivals.tasks import refresh_resource

pytestmark = pytest.mark.django_db
CONTEXT = {"season": "2026/27", "scoring_gameweek": 10, "stale": False}


def page(number, has_next=True, entry=None):
    return {
        "league": {"name": "Updated league"},
        "standings": {
            "has_next": has_next,
            "results": [
                {
                    "entry": entry or number,
                    "entry_name": f"Team {number}",
                    "player_name": "Manager",
                    "rank": number,
                    "total": 100 + number,
                }
            ],
        },
    }


@pytest.fixture
def state():
    league = MiniLeague.objects.create(fpl_league_id="123", name="Saved league")
    team = Team.objects.create(fpl_team_id="999", team_name="Previous team")
    MiniLeagueTeam.objects.create(
        mini_league=league, team=team, current_rank=1, total_points=500
    )
    return SyncState.objects.create(
        key=f"league:{league.pk}",
        kind="league",
        object_id=league.pk,
        generation=1,
        status="queued",
        last_success_at=timezone.now() - timezone.timedelta(days=1),
        payload={"stamp": "2026/27:9"},
    )


def run(state, api):
    state.refresh_from_db()
    with (
        patch("rivals.tasks.FplApiService", return_value=api),
        patch("rivals.tasks.GameweekContextService.cached", return_value=CONTEXT),
    ):
        refresh_resource.now(state.key, state.generation)
    state.refresh_from_db()


def test_more_than_ten_pages_resume_and_publish_only_when_complete(state):
    api = Mock()
    api.fetch_mini_league_page.side_effect = [page(n, n < 12) for n in range(1, 13)]
    previous = state.last_success_at
    for expected_next in (6, 11):
        run(state, api)
        assert state.status == "queued"
        assert state.last_success_at == previous
        assert state.payload == {"stamp": "2026/27:9"}
        assert LeagueImport.objects.get().next_page == expected_next
        league = MiniLeague.objects.get(pk=state.object_id)
        assert league.name == "Saved league"
        assert league.mini_league_teams.get().total_points == 500
        assert Team.objects.count() == 1
        assert not enqueue_refresh("league", league.pk, force=True, context=CONTEXT)
    # A new service instance/process resumes from the database on every call.
    run(state, api)
    assert state.status == "ok" and state.last_success_at > previous
    assert state.payload == {"stamp": "2026/27:10"}
    assert not LeagueImport.objects.exists()
    league.refresh_from_db()
    assert league.name == "Updated league"
    assert league.mini_league_teams.count() == 12
    assert not league.mini_league_teams.filter(team__fpl_team_id="999").exists()
    assert [c.args[1] for c in api.fetch_mini_league_page.call_args_list] == list(
        range(1, 13)
    )


def test_failed_page_and_expired_lease_resume_without_losing_checkpoint(state):
    api = Mock()
    api.fetch_mini_league_page.side_effect = [page(1), None]
    run(state, api)
    assert state.status == "error" and state.failures == 1
    assert LeagueImport.objects.get().next_page == 2
    assert not enqueue_refresh("league", state.object_id, force=True, context=CONTEXT)
    SyncState.objects.filter(pk=state.pk).update(
        next_attempt_at=timezone.now() - timezone.timedelta(seconds=1)
    )
    assert enqueue_refresh("league", state.object_id, context=CONTEXT)
    state.refresh_from_db()
    abandoned_generation = state.generation
    SyncState.objects.filter(pk=state.pk).update(
        lease_until=timezone.now() - timezone.timedelta(seconds=1)
    )
    assert enqueue_refresh("league", state.object_id, context=CONTEXT)
    with patch("rivals.tasks.ingest") as ingest:
        refresh_resource.now(state.key, abandoned_generation)
        ingest.assert_not_called()
    api.fetch_mini_league_page.side_effect = [page(2, False)]
    run(state, api)
    assert state.status == "ok"
    assert api.fetch_mini_league_page.call_args.args[1] == 2


@pytest.mark.parametrize("restart", ["season", "gameweek", "expired"])
def test_incompatible_or_expired_checkpoint_restarts_at_page_one(state, restart):
    api = Mock()
    api.fetch_mini_league_page.side_effect = [page(n) for n in range(1, 6)]
    run(state, api)
    checkpoint = LeagueImport.objects.get()
    if restart == "expired":
        LeagueImport.objects.filter(pk=checkpoint.pk).update(
            created_at=timezone.now() - timezone.timedelta(days=2)
        )
    else:
        checkpoint.stamp = "2025/26:10" if restart == "season" else "2026/27:9"
        checkpoint.save()
    api.fetch_mini_league_page.side_effect = [page(1, False)]
    run(state, api)
    assert state.status == "ok"
    assert api.fetch_mini_league_page.call_args.args[1] == 1


def test_publication_failure_rolls_back_and_retries_without_http(state):
    api = Mock()
    api.fetch_mini_league_page.return_value = page(1, False)
    with patch.object(MiniLeague, "save", side_effect=RuntimeError("write failed")):
        run(state, api)
    assert state.status == "error"
    assert Team.objects.count() == 1
    league = MiniLeague.objects.get(pk=state.object_id)
    assert league.name == "Saved league"
    assert league.mini_league_teams.get().total_points == 500
    assert LeagueImport.objects.get().next_page == 0
    SyncState.objects.filter(pk=state.pk).update(next_attempt_at=None)
    assert enqueue_refresh("league", state.object_id, context=CONTEXT)
    api.fetch_mini_league_page.reset_mock()
    run(state, api)
    assert state.status == "ok"
    api.fetch_mini_league_page.assert_not_called()


def test_duplicate_boundary_is_reconciled_before_publication(state):
    api = Mock()
    api.fetch_mini_league_page.side_effect = [page(1), page(2, False, entry=1)]
    run(state, api)
    assert state.status == "queued"
    checkpoint = LeagueImport.objects.get()
    assert checkpoint.league_info["_rivals_pagination"]["expected"] == 2
    assert Team.objects.count() == 1
    # The tied entries swap ordering on a later read; retain their union.
    api.fetch_mini_league_page.side_effect = [page(1), page(2, False)]
    run(state, api)
    assert state.status == "ok"
    assert not LeagueImport.objects.exists()
    members = MiniLeagueTeam.objects.filter(mini_league_id=state.object_id)
    assert set(members.values_list("team__fpl_team_id", flat=True)) == {"1", "2"}
    assert members.get(team__fpl_team_id="1").total_points == 101


def test_unresolved_duplicates_retain_checkpoint_and_back_off(state):
    api = Mock()
    api.fetch_mini_league_page.side_effect = [page(1), page(2, False, entry=1)]
    previous = state.last_success_at
    run(state, api)
    for _ in range(LeagueImportService.REPAIR_ROUNDS):
        api.fetch_mini_league_page.side_effect = [page(1), page(2, False, entry=1)]
        run(state, api)
    assert state.status == "error" and state.next_attempt_at > timezone.now()
    assert state.last_success_at == previous
    assert Team.objects.count() == 1
    checkpoint = LeagueImport.objects.get()
    assert checkpoint.pages.count() == 2
    assert checkpoint.league_info["_rivals_pagination"]["pending"] == [1, 2]
    assert not enqueue_refresh("league", state.object_id, force=True, context=CONTEXT)


def test_repair_outage_resumes_at_failed_boundary_page(state):
    api = Mock()
    api.fetch_mini_league_page.side_effect = [page(1), page(2, False, entry=1)]
    run(state, api)
    api.fetch_mini_league_page.side_effect = [page(1), None]
    run(state, api)
    assert state.status == "error"
    assert LeagueImport.objects.get().league_info["_rivals_pagination"]["pending"] == [
        2
    ]
    SyncState.objects.filter(pk=state.pk).update(next_attempt_at=None)
    assert enqueue_refresh("league", state.object_id, context=CONTEXT)
    api.fetch_mini_league_page.reset_mock()
    api.fetch_mini_league_page.side_effect = [page(2, False)]
    run(state, api)
    assert state.status == "ok"
    assert [call.args[1] for call in api.fetch_mini_league_page.call_args_list] == [2]


def test_reconciled_publication_retry_needs_no_more_http(state):
    api = Mock()
    api.fetch_mini_league_page.side_effect = [page(1), page(2, False, entry=1)]
    run(state, api)
    api.fetch_mini_league_page.side_effect = [page(1), page(2, False)]
    with patch.object(MiniLeague, "save", side_effect=RuntimeError("write failed")):
        run(state, api)
    assert state.status == "error" and Team.objects.count() == 1
    SyncState.objects.filter(pk=state.pk).update(next_attempt_at=None)
    assert enqueue_refresh("league", state.object_id, context=CONTEXT)
    api.fetch_mini_league_page.reset_mock()
    run(state, api)
    assert state.status == "ok"
    api.fetch_mini_league_page.assert_not_called()


def test_changed_membership_during_repair_restarts_without_publication(state):
    api = Mock()
    api.fetch_mini_league_page.side_effect = [page(1), page(2, False, entry=1)]
    run(state, api)
    api.fetch_mini_league_page.side_effect = [page(2), page(3, False)]
    run(state, api)
    assert state.status == "error"
    assert not LeagueImport.objects.exists()
    assert Team.objects.count() == 1


def test_large_tie_group_repair_yields_after_five_pages(state):
    def tied_page(number, final=False, entry=None):
        response = page(number, not final, entry=entry)
        response["standings"]["results"][0].update(rank=1, total=100)
        return response

    api = Mock()
    api.fetch_mini_league_page.side_effect = [
        tied_page(n, n == 12, entry=1 if n == 12 else n) for n in range(1, 13)
    ]
    for _ in range(3):
        run(state, api)
    assert state.status == "queued" and Team.objects.count() == 1
    assert LeagueImport.objects.get().league_info["_rivals_pagination"][
        "candidates"
    ] == list(range(1, 13))
    for start, end in ((1, 6), (6, 11), (11, 13)):
        api.fetch_mini_league_page.reset_mock()
        api.fetch_mini_league_page.side_effect = [
            tied_page(n, n == 12) for n in range(start, end)
        ]
        run(state, api)
        assert api.fetch_mini_league_page.call_count == end - start
        assert state.status == ("ok" if end == 13 else "queued")
    assert MiniLeagueTeam.objects.filter(mini_league_id=state.object_id).count() == 12


@pytest.mark.parametrize(
    "invalid", ["team", "points", "empty_page", "missing_has_next", "wrong_page"]
)
def test_invalid_page_is_not_checkpointed(state, invalid):
    response = page(1)
    if invalid == "team":
        response["standings"]["results"] = [None]
    elif invalid == "points":
        response["standings"]["results"][0]["total"] = None
    elif invalid == "empty_page":
        response["standings"]["results"] = []
    elif invalid == "missing_has_next":
        del response["standings"]["has_next"]
    else:
        response["standings"]["page"] = 2
    api = Mock()
    api.fetch_mini_league_page.return_value = response
    run(state, api)
    assert state.status == "error"
    checkpoint = LeagueImport.objects.get()
    assert checkpoint.next_page == 1 and not checkpoint.pages.exists()
    assert Team.objects.count() == 1


def test_lease_replaced_during_http_cannot_checkpoint_or_publish(state):
    api = Mock()

    def replace_lease(*args):
        SyncState.objects.filter(pk=state.pk).update(generation=2, status="queued")
        return page(1, False)

    api.fetch_mini_league_page.side_effect = replace_lease
    run(state, api)
    assert state.generation == 2 and state.status == "queued"
    assert LeagueImport.objects.get().next_page == 1
    assert not LeagueImport.objects.get().pages.exists()
    assert Team.objects.count() == 1


def test_failed_continuation_enqueue_retains_checkpoint_for_retry(state):
    api = Mock()
    api.fetch_mini_league_page.side_effect = [page(n) for n in range(1, 6)]
    execute = refresh_resource.now
    with (
        patch("rivals.tasks.FplApiService", return_value=api),
        patch("rivals.tasks.GameweekContextService.cached", return_value=CONTEXT),
        patch(
            "rivals.tasks.refresh_resource", side_effect=RuntimeError("queue failed")
        ),
    ):
        execute(state.key, state.generation)
    state.refresh_from_db()
    assert state.status == "error" and state.generation == 1
    assert state.lease_until is None
    assert LeagueImport.objects.get().next_page == 6


def test_background_runner_leaves_a_runnable_continuation(state):
    import json

    from background_task.models import Task
    from background_task.tasks import tasks

    api = Mock()
    api.fetch_mini_league_page.side_effect = [page(n) for n in range(1, 6)]
    refresh_resource(state.key, state.generation)
    with (
        patch("rivals.tasks.FplApiService", return_value=api),
        patch("rivals.tasks.GameweekContextService.cached", return_value=CONTEXT),
    ):
        assert tasks.run_next_task()
    continuation = Task.objects.get(task_name="rivals.tasks.refresh_resource")
    assert json.loads(continuation.task_params) == [[state.key, 2], {}]
    state.refresh_from_db()
    assert state.generation == 2 and state.status == "queued"
    assert LeagueImport.objects.get().next_page == 6


def test_bulk_publication_across_multiple_database_batches(state):
    api = Mock()
    responses = []
    for number in range(1, 13):
        response = page(number, number < 12)
        response["standings"]["results"] = [
            page(entry)["standings"]["results"][0]
            for entry in range((number - 1) * 100 + 1, number * 100 + 1)
        ]
        responses.append(response)
    api.fetch_mini_league_page.side_effect = responses
    for _ in range(3):
        run(state, api)
    assert state.status == "ok"
    assert MiniLeagueTeam.objects.filter(mini_league_id=state.object_id).count() == 1200
    # Existing shared teams are updated rather than duplicated.
    assert Team.objects.count() == 1200


@pytest.mark.parametrize(
    "old_season,expected_previous", [("2026/27", 7), ("2025/26", None)]
)
def test_existing_team_and_rank_are_updated_with_season_isolation(
    state, old_season, expected_previous
):
    member = MiniLeagueTeam.objects.get(mini_league_id=state.object_id)
    member.season = old_season
    member.current_rank = 7
    member.save()
    api = Mock()
    api.fetch_mini_league_page.return_value = page(1, False, entry=999)
    run(state, api)
    member.refresh_from_db()
    member.team.refresh_from_db()
    assert member.previous_rank == expected_previous
    assert member.current_rank == 1 and member.total_points == 101
    assert member.season == "2026/27"
    assert member.team.team_name == "Team 1"
    assert state.status == "ok"
