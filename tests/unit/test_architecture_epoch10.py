"""Limits, cross-process publications, and season-safe historical identities."""

import importlib
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import IntegrityError, connection, transaction
from django.urls import reverse
from django.utils import timezone

from rivals.models import (
    FplSnapshot,
    LeagueImport,
    LeagueImportPage,
    LeagueStandingsPage,
    MiniLeague,
    MiniLeagueTeam,
    Player,
    SquadRefresh,
    SyncState,
    Team,
    Transfer,
    TransferPlan,
    UserMiniLeague,
)
from rivals.services.decision_engine import DecisionEngine, DecisionUnavailable
from rivals.services.fpl_api_service import FplApiService
from rivals.services.fpl_snapshot_service import publish_snapshots, read_snapshot
from rivals.services.league_import_service import LeagueImportService
from rivals.services.player_sync_service import PlayerSyncService
from rivals.services.squad_history_service import SquadHistoryService
from rivals.services.team_analysis_service import TeamAnalysisService
from rivals.services.transfer_player_association_service import (
    TransferPlayerAssociationService,
)
from rivals.tasks import refresh_resource
from tests.unit.test_epoch3_squads import EVENTS, NOW, api, import_rows, manager
from tests.unit.test_league_import import page

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("route", ["rivals:teams.sync", "api:team_sync"])
def test_manual_sync_refreshes_lineup_even_when_recently_synced(client, manager, route):
    authenticate(client, manager)
    target = SquadRefresh.objects.create(
        team=manager.team, season="2026/27", gameweek=10
    )
    state = SyncState.objects.create(
        key=f"squad:{target.pk}",
        kind="squad",
        object_id=target.pk,
        status="ok",
        last_success_at=NOW,
        payload={"stamp": "2026/27:10"},
    )

    response = client.post(reverse(route, args=[manager.team_id]))

    assert response.status_code in (202, 302)
    state.refresh_from_db()
    assert state.status == "queued"
    assert state.generation == 1
    assert (
        SyncState.objects.filter(
            kind__in=["profile", "history", "transfers"], status="queued"
        ).count()
        == 3
    )


@pytest.mark.parametrize("method", ["compare_teams", "quick_compare"])
@pytest.mark.parametrize("expired_snapshot", [False, True])
def test_comparisons_reuse_saved_lineups_after_picks_snapshot_expires(
    manager, api, method, expired_snapshot
):
    rival = Team.objects.create(fpl_team_id="102", team_name="Rival XI")
    for team in (manager.team, rival):
        import_rows(team, api)
    snapshots = {
        "bootstrap": api.fetch_bootstrap_static.return_value,
        "fixtures": api.fetch_fixtures_data.return_value,
    }
    if expired_snapshot:
        for entry in (101, 102):
            snapshots[f"picks:2026/27:{entry}:10"] = api.fetch_team_picks.return_value
    publish_snapshots(snapshots)
    FplSnapshot.objects.filter(key__startswith="picks:").update(expires_at=NOW)

    service = TeamAnalysisService()
    result = getattr(service, method)(101, 102)

    assert "error" not in result
    assert result["current_gameweek"] == 10
    assert result["captaincy"]["mine"]["name"] == "Player 1"
    assert result["captaincy"]["rival"]["name"] == "Player 1"
    if method == "compare_teams":
        assert result["head_to_head_advantages"]["bank_balance"] is None
    assert (
        service._comparison_picks(101, 10)["picks"]
        == api.fetch_team_picks.return_value["picks"]
    )
    assert SyncState.objects.filter(kind="squad", status="queued").count() == 2


@pytest.mark.parametrize("method", ["compare_teams", "quick_compare"])
@pytest.mark.parametrize("unavailable", ["season", "gameweek", "incomplete"])
def test_comparisons_never_substitute_wrong_or_incomplete_lineups(
    manager, api, method, unavailable
):
    rival = Team.objects.create(fpl_team_id="102", team_name="Rival XI")
    import_rows(manager.team, api)
    import_rows(
        rival,
        api,
        season="2025/26" if unavailable == "season" else "2026/27",
        gameweek=9 if unavailable == "gameweek" else 10,
    )
    if unavailable == "incomplete":
        rival.squad_histories.filter(position=15).delete()
    publish_snapshots(
        {"bootstrap": api.fetch_bootstrap_static.return_value, "fixtures": []}
    )

    result = getattr(TeamAnalysisService(), method)(101, 102)

    assert (
        result["error"] == "Team lineups are being refreshed. Please try again shortly."
    )
    assert SyncState.objects.filter(kind="squad", status="queued").count() == 2


@pytest.fixture(autouse=True)
def isolated():
    cache.clear()
    with (
        patch("django.utils.timezone.now", return_value=NOW),
        patch("requests.get", side_effect=AssertionError("Web request attempted HTTP")),
    ):
        yield
    cache.clear()


def authenticate(client, manager):
    manager.is_verified = True
    manager.save(update_fields=["is_verified"])
    client.force_login(manager)


def league_with_rows(manager, size=205):
    league = MiniLeague.objects.create(fpl_league_id="88", name="Friends")
    UserMiniLeague.objects.create(user=manager, mini_league=league)
    teams = Team.objects.bulk_create(
        [Team(fpl_team_id=str(1000 + i), team_name=f"Team {i}") for i in range(size)]
    )
    MiniLeagueTeam.objects.bulk_create(
        [
            MiniLeagueTeam(
                mini_league=league, team=team, current_rank=i + 1, total_points=500 - i
            )
            for i, team in enumerate(teams)
        ]
    )
    return league


def test_shared_global_snapshots_survive_local_cache_clear_and_pair_reads(manager, api):
    bootstrap = api.fetch_bootstrap_static.return_value
    publish_snapshots({"bootstrap": bootstrap, "fixtures": []})
    reader = FplApiService(snapshot_only=True)
    assert reader.fetch_bootstrap_static() == bootstrap
    # A single reader retains the same publication pair during a worker commit.
    publish_snapshots({"bootstrap": {"events": []}, "fixtures": [{"new": True}]})
    assert reader.fetch_fixtures_data() == []
    cache.clear()
    other_process = FplApiService(snapshot_only=True)
    assert other_process.fetch_fixtures_data() == [{"new": True}]


def test_expired_snapshot_queues_once_and_never_uses_local_cache(manager):
    publish_snapshots({"bootstrap": {"events": EVENTS}, "fixtures": []})
    FplSnapshot.objects.update(expires_at=NOW - timezone.timedelta(seconds=1))
    cache.set(FplApiService.BOOTSTRAP_CACHE_KEY, {"unsafe": "old local data"})
    for _ in range(3):
        assert FplApiService(snapshot_only=True).fetch_bootstrap_static() is None
    state = SyncState.objects.get(key="context")
    assert state.status == "queued" and state.generation == 1


def test_context_worker_publishes_global_pair_atomically(manager, api):
    state = SyncState.objects.get(key="context")
    state.status, state.generation = "queued", 1
    state.save()
    api.fetch_fixtures_data.return_value = []
    api.SEASON_CACHE_KEY = FplApiService.SEASON_CACHE_KEY
    api.SEASON_CACHE_TIMEOUT = FplApiService.SEASON_CACHE_TIMEOUT
    with patch("rivals.tasks.FplApiService", return_value=api):
        refresh_resource.now("context", 1)
    assert (
        FplApiService(snapshot_only=True).fetch_bootstrap_static()
        == api.fetch_bootstrap_static.return_value
    )
    assert FplApiService(snapshot_only=True).fetch_fixtures_data() == []
    state.refresh_from_db()
    assert "_snapshots" not in state.payload
    assert state.status == "ok"


def test_superseded_worker_cannot_publish_shared_snapshots(manager):
    state = SyncState.objects.create(
        key="players", kind="players", status="queued", generation=1
    )

    def superseded(_):
        SyncState.objects.filter(pk=state.pk).update(generation=2, status="queued")
        return {"_snapshots": {"bootstrap": {"obsolete": True}}}

    with patch("rivals.tasks.ingest", side_effect=superseded):
        refresh_resource.now("players", 1)
    assert not FplSnapshot.objects.exists()


def test_failed_context_retains_last_publication(manager, api):
    publish_snapshots({"bootstrap": {"saved": True}})
    state = SyncState.objects.get(key="context")
    state.status, state.generation = "queued", 1
    state.save()
    api.fetch_bootstrap_static.return_value = None
    with patch("rivals.tasks.FplApiService", return_value=api):
        refresh_resource.now("context", 1)
    assert read_snapshot("bootstrap") == {"saved": True}
    state.refresh_from_db()
    assert state.status == "error"


def test_lineup_read_queues_then_worker_fills_snapshot(manager, api):
    import_rows(manager.team, api, points=False)
    rows, notice = SquadHistoryService(manager.team).load("2026/27", 10)
    assert len(rows) == 15 and notice
    target = SquadRefresh.objects.get()
    state = SyncState.objects.get(key=f"squad:{target.pk}")
    with patch("rivals.tasks.FplApiService", return_value=api):
        refresh_resource.now(state.key, state.generation)
    state.refresh_from_db()
    assert state.status == "ok"
    rows, notice = SquadHistoryService(manager.team).load("2026/27", 10)
    assert notice is None and all(row.gameweek_points is not None for row in rows)
    assert api.fetch_team_picks.call_count == 1


def test_revoked_squad_job_cannot_replace_saved_lineup(manager, api):
    import_rows(manager.team, api, points=False)
    SquadHistoryService(manager.team).load("2026/27", 10)
    state = SyncState.objects.get(kind="squad")
    original = api.fetch_gameweek_live.return_value

    def revoke(*_):
        SyncState.objects.filter(pk=state.pk).update(
            generation=state.generation + 1, status="queued"
        )
        return original

    api.fetch_gameweek_live.side_effect = revoke
    with patch("rivals.tasks.FplApiService", return_value=api):
        refresh_resource.now(state.key, state.generation)
    assert (
        manager.team.squad_histories.filter(gameweek_points__isnull=True).count() == 15
    )


def test_default_planner_loads_published_data_without_http(manager, api):
    import_rows(manager.team, api)
    publish_snapshots(
        {"bootstrap": api.fetch_bootstrap_static.return_value, "fixtures": []}
    )
    cache.clear()
    assert len(DecisionEngine(manager).load().public_ids) == 15


def test_cold_planner_queues_global_refresh(manager):
    with pytest.raises(DecisionUnavailable):
        DecisionEngine(manager).load()
    assert SyncState.objects.get(key="context").status == "queued"


@pytest.mark.parametrize(
    "route",
    [
        "accounts:profile",
        "rivals:teams.score",
        "rivals:teams.lineup",
        "rivals:teams.analysis",
        "rivals:teams.compare.quick",
        "rivals:decisions.preview",
        "core:index",
    ],
)
def test_cold_public_and_authenticated_reads_never_call_http(client, manager, route):
    authenticate(client, manager)
    url = reverse(
        route, args=[manager.team_id] if route.startswith("rivals:teams.") else []
    )
    response = client.get(url)
    assert response.status_code in (200, 400, 503)


def full_player(name):
    return {
        "id": 1,
        "web_name": name,
        "first_name": name,
        "second_name": "Test",
        "element_type": 3,
        "team": 1,
        "now_cost": 50,
        "total_points": 20,
        "status": "a",
        "selected_by_percent": "1.0",
        "form": "1.0",
        "photo": "1.jpg",
        "news": "",
        "minutes": 90,
        "goals_scored": 1,
        "assists": 0,
        "clean_sheets": 0,
        "goals_conceded": 0,
        "yellow_cards": 0,
        "red_cards": 0,
        "bonus": 0,
        "expected_goals": "1.0",
        "expected_assists": "1.0",
        "ict_index": "1.0",
    }


def test_player_rollover_keeps_previous_identity_and_transfer_names(manager, api):
    old = Player.objects.create(
        season="2025/26", fpl_id=1, web_name="Old identity", element_type=3
    )
    transfer = Transfer.objects.create(
        team=manager.team,
        season="2025/26",
        gameweek=1,
        player_in=old,
        player_in_name="Historical name",
        player_out_name="Outgoing",
    )
    api.fetch_bootstrap_static.return_value = {
        "events": EVENTS,
        "elements": [full_player("New identity")],
    }
    assert PlayerSyncService(api).sync_all_players()["success"]
    old.refresh_from_db()
    assert old.web_name == "Old identity"
    assert Player.objects.get(season="2026/27", fpl_id=1).web_name == "New identity"
    assert transfer.player_in_display_name == "Historical name"
    assert "Historical name" in transfer.transfer_summary
    with pytest.raises(IntegrityError), transaction.atomic():
        Player.objects.create(
            season="2026/27", fpl_id=1, web_name="Duplicate", element_type=3
        )


def test_invalid_player_batch_rolls_back_all_player_changes(manager, api):
    api.fetch_bootstrap_static.return_value = {
        "events": EVENTS,
        "elements": [full_player("Valid"), {"id": 2}],
    }
    assert not PlayerSyncService(api).sync_all_players()["success"]
    assert not Player.objects.exists()


def test_player_season_comes_from_same_bootstrap_payload(manager, api):
    api.fetch_current_season.return_value = "2099/00"
    api.fetch_bootstrap_static.return_value = {
        "events": EVENTS,
        "elements": [full_player("Name")],
    }
    assert PlayerSyncService(api).sync_all_players()["success"]
    assert Player.objects.get().season == "2026/27"
    api.fetch_current_season.assert_not_called()


def test_transfer_association_never_crosses_seasons_or_uses_ambiguous_names(manager):
    Player.objects.create(season="2026/27", fpl_id=1, web_name="Shared", element_type=3)
    Player.objects.create(season="2026/27", fpl_id=2, web_name="Shared", element_type=3)
    transfer = Transfer.objects.create(
        team=manager.team,
        season="2025/26",
        gameweek=1,
        player_in_fpl_id=1,
        player_in_name="Player #1",
        player_out_name="Shared",
    )
    service = TransferPlayerAssociationService()
    service.associate_all_transfers("2025/26")
    transfer.refresh_from_db()
    assert transfer.player_in_id is None and transfer.player_out_id is None
    assert service.find_player_by_name("Shared", "2026/27") is None
    assert service.find_player_by_name("Shared") is None


def test_transfer_association_fills_only_placeholders_and_is_bounded(
    manager, monkeypatch
):
    player = Player.objects.create(
        season="2026/27", fpl_id=1, web_name="Resolved", element_type=3
    )
    transfers = [
        Transfer.objects.create(
            team=manager.team,
            season="2026/27",
            gameweek=1,
            player_in_fpl_id=1,
            player_in_name=name,
            player_out_name="Unknown",
        )
        for name in ("Saved name", "Player #1", "Player #1")
    ]
    monkeypatch.setattr(TransferPlayerAssociationService, "BATCH_SIZE", 2)
    service = TransferPlayerAssociationService()
    first = service.associate_all_transfers("2026/27")
    assert first["associated_count"] == 2 and first["next_cursor"] == transfers[1].pk
    service.associate_all_transfers("2026/27", after_pk=first["next_cursor"])
    assert list(
        Transfer.objects.order_by("pk").values_list("player_in_name", flat=True)
    ) == ["Saved name", "Resolved", "Resolved"]
    assert Transfer.objects.filter(player_in=player).count() == 3


def test_migration_preserves_numeric_transfer_ids_without_guessing_season(manager):
    player = Player.objects.create(fpl_id=123, web_name="Legacy", element_type=3)
    transfer = Transfer.objects.create(
        team=manager.team,
        gameweek=1,
        season="2024/25",
        player_in=player,
        player_in_name="Legacy",
        player_out_name="Old",
    )
    migration = importlib.import_module(
        "rivals.migrations.0021_player_season_transfer_player_in_fpl_id_and_more"
    )
    migration.preserve_transfer_elements(apps, SimpleNamespace(connection=connection))
    transfer.refresh_from_db()
    player.refresh_from_db()
    assert transfer.player_in_fpl_id == 123 and player.season == ""


def test_league_api_and_html_are_paginated_deterministically(client, manager):
    league = league_with_rows(manager)
    authenticate(client, manager)
    first = client.get(reverse("api:league_detail", args=[league.pk])).json()
    second = client.get(
        reverse("api:league_detail", args=[league.pk]), {"page": 2, "page_size": 100000}
    ).json()
    assert len(first["standings"]) == len(second["standings"]) == 100
    assert first["pagination"]["count"] == 205
    assert not {r["id"] for r in first["standings"]} & {
        r["id"] for r in second["standings"]
    }
    response = client.get(
        reverse("rivals:minileagues.show", args=[league.pk]), {"page": 3}
    )
    assert len(response.context["standings"]) == 5
    assert b"Page 3 of 3" in response.content


def test_transfer_history_api_has_bounded_navigation(client, manager):
    Transfer.objects.bulk_create(
        [
            Transfer(
                team=manager.team,
                season="2026/27",
                gameweek=1,
                player_in_name="In",
                player_out_name="Out",
            )
            for _ in range(205)
        ]
    )
    authenticate(client, manager)
    data = client.get(
        reverse("api:team_transfers", args=[manager.team_id]), {"page_size": 100000}
    ).json()
    assert data["count"] == 205 and len(data["results"]) == 100 and data["next"]
    assert len(client.get(data["next"]).json()["results"]) == 100


def test_import_rejects_oversized_page_before_persistence(manager):
    data = page(1)
    data["standings"]["results"] *= 101
    with pytest.raises(RuntimeError):
        LeagueImportService._validated_rows(data, 1)


def test_hard_import_ceiling_switches_to_summary_despite_high_configuration(
    manager, settings
):
    settings.LEAGUE_FULL_IMPORT_LIMIT = 1000000
    league = MiniLeague.objects.create(fpl_league_id="88", name="Old")
    state = SyncState.objects.create(
        key=f"league:{league.pk}",
        kind="league",
        object_id=league.pk,
        generation=1,
        status="running",
    )
    run = LeagueImport.objects.create(
        league=league, stamp="2026/27:10", next_page=11, league_info={"name": "League"}
    )
    for number in range(1, 11):
        LeagueImportPage.objects.create(
            import_run=run,
            number=number,
            standings=[
                {**page(i)["standings"]["results"][0], "entry": (number - 1) * 100 + i}
                for i in range(1, 101)
            ],
        )
    upstream = Mock()
    upstream.fetch_mini_league_page.return_value = page(11)
    assert LeagueImportService(
        state, upstream, {"season": "2026/27", "scoring_gameweek": 10}
    ).run_batch()
    league.refresh_from_db()
    assert league.summary_only and not MiniLeagueTeam.objects.exists()
    assert not LeagueImport.objects.exists()


def test_planned_transfer_rejects_cross_season_player(client, manager):
    authenticate(client, manager)
    plan = TransferPlan.objects.create(
        user=manager, team=manager.team, season="2026/27"
    )
    player = Player.objects.create(
        season="2025/26", fpl_id=1, web_name="Old", element_type=3
    )
    response = client.post(
        reverse("api:plan_transfer_create", args=[plan.pk]),
        {"player_in": player.pk, "player_out": player.pk, "gameweek": 11},
    )
    assert response.status_code == 400 and not plan.transfers.exists()


def test_signup_upstream_failure_is_a_form_error(client):
    with patch("accounts.forms.FplApiService.fetch_team_basic_info", return_value=None):
        response = client.post(
            reverse("accounts:signup"),
            {
                "fpl_id": 123,
                "email": "new@example.org",
                "password1": "Secure-password-4789",
                "password2": "Secure-password-4789",
            },
        )
    assert response.status_code == 200
    assert b"Could not validate" in response.content
    assert not get_user_model().objects.filter(email="new@example.org").exists()


def test_large_league_page_demand_has_shared_queue_ceiling(manager):
    from rivals.services.league_summary_service import requested_page

    league = MiniLeague.objects.create(
        fpl_league_id="88", name="Huge", rank_count=100000
    )
    for number in range(2, 30):
        requested_page(league, "2026/27", number)
    assert LeagueStandingsPage.objects.filter(mini_league=league).count() == 6
    assert SyncState.objects.filter(kind="league_page").count() == 6


def test_dispatcher_uses_resumable_bounded_batches(manager):
    from rivals.tasks import refresh_tick

    teams = Team.objects.bulk_create(
        [Team(fpl_team_id=str(1000 + i), team_name="Team") for i in range(104)]
    )
    users = [
        get_user_model()(
            username=f"owner-{i}", email=f"owner-{i}@example.org", team=team
        )
        for i, team in enumerate(teams)
    ]
    get_user_model().objects.bulk_create(users)
    with (
        patch("rivals.services.refresh_service.enqueue_team") as enqueue,
        patch("rivals.services.deadline_digest_service.dispatch_digests"),
    ):
        refresh_tick.now()
        first = {call.args[0] for call in enqueue.call_args_list}
        assert len(first) == 100
        enqueue.reset_mock()
        refresh_tick.now()
        second = {call.args[0] for call in enqueue.call_args_list}
        assert len(second) == 5 and not first & second
        assert SyncState.objects.get(key="worker").payload["team_cursor"] == 0


def test_real_worker_transport_publishes_picks_for_subsequent_planning(manager, api):
    import_rows(manager.team, api, points=False)
    SquadHistoryService(manager.team).load("2026/27", 10)
    state = SyncState.objects.get(kind="squad")

    def response(data):
        result = Mock()
        result.json.return_value = data
        return result

    with patch(
        "requests.get",
        side_effect=[
            response(api.fetch_bootstrap_static.return_value),
            response(api.fetch_team_picks.return_value),
            response(api.fetch_gameweek_live.return_value),
        ],
    ) as http:
        refresh_resource.now(state.key, state.generation)
    assert http.call_count == 3
    state.refresh_from_db()
    assert state.status == "ok"
    assert read_snapshot("picks:2026/27:101:10")["picks"]
    assert read_snapshot("live:2026/27:10")["elements"]
    publish_snapshots(
        {"bootstrap": api.fetch_bootstrap_static.return_value, "fixtures": []}
    )
    cache.clear()
    assert len(DecisionEngine(manager, fresh_squad=True).load().public_ids) == 15


def test_profile_snapshots_are_published_by_transport_and_reused(manager):
    payload = {"id": 101, "name": "Snapshot team"}
    response = Mock()
    response.json.return_value = payload
    api = FplApiService()
    with patch("requests.get", return_value=response):
        assert api.fetch_team_basic_info(101) == payload
    assert api.snapshots == {"profile:2026/27:101": payload}
    publish_snapshots(api.snapshots)
    cache.clear()
    assert FplApiService(snapshot_only=True).fetch_team_basic_info(101) == payload


def test_score_refresh_preserves_first_imported_player_metadata(manager, api):
    from rivals.services.team_sync_service import TeamSyncService

    import_rows(manager.team, api, points=False)
    original = manager.team.squad_histories.get(player_fpl_id=1)
    elements = deepcopy(api.fetch_bootstrap_static.return_value["elements"])
    for element in elements:
        element.update(web_name="Changed name", now_cost=999, total_points=9999)
    assert TeamSyncService(manager.team).sync_squad_for_gameweek(
        10,
        picks=api.fetch_team_picks.return_value,
        season="2026/27",
        refresh=True,
        elements={p["id"]: p for p in elements},
        points={p["id"]: 42 for p in elements},
    )
    refreshed = manager.team.squad_histories.get(player_fpl_id=1)
    assert refreshed.player_name == original.player_name
    assert refreshed.player_cost == original.player_cost
    assert refreshed.total_points_at_time == original.total_points_at_time
    assert refreshed.gameweek_points == 42


def test_superseded_player_job_cannot_update_catalogue(manager, api):
    api.fetch_bootstrap_static.return_value = {
        "events": EVENTS,
        "elements": [full_player("New")],
    }

    def revoked():
        raise RuntimeError("Lease superseded")

    assert not PlayerSyncService(api, publication_guard=revoked).sync_all_players()[
        "success"
    ]
    assert not Player.objects.exists()


def test_reader_retains_season_identity_when_context_rolls_over(manager, api):
    publish_snapshots(
        {
            "bootstrap": api.fetch_bootstrap_static.return_value,
            "fixtures": [],
            "picks:2026/27:101:10": {"saved": True},
        }
    )
    reader = FplApiService(snapshot_only=True)
    reader.fetch_bootstrap_static()
    context = SyncState.objects.get(key="context")
    context.payload = {"events": [{"id": 1, "deadline_time": "2027-08-15T12:00:00Z"}]}
    context.save()
    publish_snapshots({"picks:2027/28:101:10": {"wrong_season": True}})
    assert reader.fetch_team_picks(101, 10) == {"saved": True}
