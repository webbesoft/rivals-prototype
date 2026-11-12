from copy import deepcopy
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from time import perf_counter
from unittest.mock import Mock, patch

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from rivals.models import (
    GameweekData,
    Player,
    SquadHistory,
    SyncState,
    Team,
    TeamPlanningInput,
)
from rivals.services.fpl_api_service import FplApiService
from rivals.services.squad_history_service import SquadHistoryService
from rivals.services.team_analysis_service import TeamAnalysisService
from rivals.services.team_sync_service import TeamSyncService

NOW = datetime(2026, 11, 10, 12, tzinfo=dt_timezone.utc)
EVENTS = [
    {"id": 1, "deadline_time": "2026-08-15T12:00:00Z", "finished": True},
    {"id": 10, "deadline_time": "2026-11-07T12:00:00Z", "finished": False},
    {"id": 11, "deadline_time": "2026-11-14T12:00:00Z", "finished": False},
]


@pytest.fixture(autouse=True)
def isolated_time_cache_and_network():
    cache.clear()
    with (
        patch("django.utils.timezone.now", return_value=NOW),
        patch("requests.get", side_effect=AssertionError("Unexpected HTTP request")),
    ):
        yield
    cache.clear()


@pytest.fixture
def manager(db):
    team = Team.objects.create(
        fpl_team_id="101", team_name="My XI", current_season="2026/27", bank=12
    )
    user = get_user_model().objects.create_user(
        username="manager",
        email="manager@example.com",
        password="test123456",
        team=team,
    )
    SyncState.objects.create(
        key="context",
        kind="context",
        status="ok",
        last_success_at=NOW,
        payload={"events": EVENTS, "fixtures": [], "stamp": "2026/27:10"},
    )
    return user


@pytest.fixture
def api():
    api = Mock(spec=FplApiService)
    positions = [1] * 2 + [2] * 5 + [3] * 5 + [4] * 3
    elements = [
        {
            "id": i,
            "web_name": f"Player {i}",
            "first_name": "Player",
            "second_name": str(i),
            "element_type": pos,
            "team": 1,
            "now_cost": 50 + i,
            "total_points": 100 + i,
            "form": "4.0",
            "points_per_game": "4.1",
            "minutes": 700,
            "expected_goals": "1.0",
            "expected_assists": "0.5",
            "selected_by_percent": "10.0",
        }
        for i, pos in enumerate(positions, 1)
    ]
    order = [1, 3, 4, 5, 6, 8, 9, 10, 11, 13, 14, 2, 7, 12, 15]
    picks = [
        {
            "element": element,
            "position": position,
            "is_captain": position == 1,
            "is_vice_captain": position == 2,
            "multiplier": 3 if position == 1 else 1 if position <= 11 else 0,
        }
        for position, element in enumerate(order, 1)
    ]
    api.fetch_bootstrap_static.return_value = {
        "events": deepcopy(EVENTS),
        "elements": elements,
        "teams": [{"id": 1, "name": "Club", "short_name": "CLB"}],
    }
    api.fetch_fixtures_data.return_value = [
        {
            "id": 1,
            "event": 11,
            "team_h": 1,
            "team_a": 2,
            "team_h_difficulty": 2,
            "team_a_difficulty": 3,
            "finished": False,
        },
        {"id": 2, "event": None, "team_h": 1, "team_a": 2, "finished": False},
    ]
    api.fetch_team_picks.return_value = {
        "picks": picks,
        "active_chip": "3xc",
        "entry_history": {"bank": 12, "value": 1010},
    }
    api.fetch_gameweek_live.return_value = {
        "elements": [
            {
                "id": i,
                "stats": {"total_points": 0 if i == 1 else -1 if i == 5 else i % 7},
            }
            for i in range(1, 16)
        ]
    }
    api.fetch_current_season.return_value = "2026/27"
    api.fetch_team_transfers.return_value = []
    api.fetch_team_history.return_value = {
        "current": [
            {
                "event": 10,
                "points": 61,
                "total_points": 601,
                "rank": 10000,
                "event_transfers": 0,
                "event_transfers_cost": 0,
                "points_on_bench": 4,
                "bank": 12,
            }
        ]
    }
    api.fetch_team_basic_info.return_value = {
        "name": "My XI",
        "player_first_name": "My",
        "player_last_name": "Manager",
        "summary_overall_points": 601,
        "summary_overall_rank": 10000,
        "summary_event_points": 61,
        "last_deadline_total_transfers": 8,
    }
    return api


def import_rows(team, api, season="2026/27", gameweek=10, points=True):
    service = TeamSyncService(team)
    assert service.sync_squad_for_gameweek(
        gameweek,
        season=season,
        picks=api.fetch_team_picks.return_value,
        elements={
            p["id"]: p for p in api.fetch_bootstrap_static.return_value["elements"]
        },
        clubs={p["id"]: p for p in api.fetch_bootstrap_static.return_value["teams"]},
        points=(
            {
                p["id"]: p["stats"]["total_points"]
                for p in api.fetch_gameweek_live.return_value["elements"]
            }
            if points
            else None
        ),
    )


@pytest.mark.django_db
def test_on_demand_import_uses_player_position_and_actual_event_points(manager, api):
    service = SquadHistoryService(manager.team, api=api)
    rows, error = service.load("2026/27", 10)
    assert error is None and len(rows) == 15
    assert rows[0].element_type == 1 and rows[1].element_type == 2
    assert rows[0].gameweek_points == 0 and rows[0].total_points_at_time == 101
    assert rows[0].multiplier == 3 and rows[11].multiplier == 0
    api.fetch_team_picks.assert_called_once_with("101", 10)
    manager.team.refresh_from_db()
    assert manager.team.current_season == "2026/27"


@pytest.mark.django_db
def test_warm_history_navigation_makes_no_api_requests(manager, api):
    import_rows(manager.team, api)
    start = perf_counter()
    rows, error = SquadHistoryService(manager.team, api=api).load("2026/27", 10)
    elapsed = perf_counter() - start
    assert len(rows) == 15 and error is None
    assert not api.method_calls
    print(
        f"Warm lineup: {elapsed * 1000:.1f} ms; upstream calls: {len(api.method_calls)}"
    )


@pytest.mark.django_db
def test_repair_partial_import_is_atomic(manager, api):
    import_rows(manager.team, api)
    manager.team.squad_histories.filter(position__gt=5).delete()
    rows, error = SquadHistoryService(manager.team, api=api).load("2026/27", 10)
    assert len(rows) == 15 and error is None
    assert manager.team.squad_histories.count() == 15


@pytest.mark.django_db
def test_missing_metadata_does_not_replace_saved_lineup(manager, api):
    import_rows(manager.team, api)
    original = list(manager.team.squad_histories.values_list("pk", flat=True))
    elements = {
        p["id"]: p
        for p in api.fetch_bootstrap_static.return_value["elements"]
        if p["id"] != 15
    }
    service = TeamSyncService(manager.team)
    assert not service.sync_squad_for_gameweek(
        10, picks=api.fetch_team_picks.return_value, elements=elements, refresh=True
    )
    assert list(manager.team.squad_histories.values_list("pk", flat=True)) == original


@pytest.mark.django_db
def test_failed_score_refresh_preserves_last_known_player_scores(manager, api):
    import_rows(manager.team, api)
    manager.team.squad_histories.update(updated_at=NOW - timedelta(minutes=10))
    api.fetch_gameweek_live.return_value = None
    rows, error = SquadHistoryService(manager.team, api=api).load("2026/27", 10)
    assert "out of date" in error
    assert rows[0].gameweek_points == 0
    assert rows[3].gameweek_points == -1


@pytest.mark.django_db
def test_archive_never_uses_current_season_api_and_filters_equal_gameweeks(
    manager, api
):
    import_rows(manager.team, api)
    import_rows(manager.team, api, season="2025/26", points=False)
    manager.team.squad_histories.filter(season="2025/26").update(
        player_name="Archived player"
    )
    service = SquadHistoryService(manager.team, api=api)
    selection = service.selection("2025/26", 10)
    rows, error = service.load(selection["season"], selection["gameweek"])
    assert len(rows) == 15 and "unavailable for this season" in error
    assert all(row.player_name == "Archived player" for row in rows)
    assert not api.method_calls
    assert manager.team.get_available_gameweeks("2025/26") == [10]


@pytest.mark.django_db
def test_cold_history_only_fetches_requested_gameweek(manager, api):
    selected = SquadHistoryService(manager.team, api=api).selection()
    assert selected["gameweek"] == 10 and selected["available_gameweeks"] == list(
        range(1, 11)
    )
    SquadHistoryService(manager.team, api=api).load("2026/27", 7)
    api.fetch_team_picks.assert_called_once_with("101", 7)
    api.fetch_team_history.assert_not_called()


@pytest.mark.django_db
def test_full_sync_reuses_each_pick_and_skips_completed_record_import(manager, api):
    for element in api.fetch_bootstrap_static.return_value["elements"]:
        Player.objects.create(
            fpl_id=element["id"],
            web_name=element["web_name"],
            element_type=element["element_type"],
            now_cost=element["now_cost"],
        )
    service = TeamSyncService(manager.team)
    service.fpl_api_service = api
    assert service.sync_full_data()
    api.fetch_team_picks.assert_called_once_with("101", 10)
    api.fetch_team_picks.reset_mock()
    service = TeamSyncService(manager.team)
    service.fpl_api_service = api
    assert service.sync_full_data()
    api.fetch_team_picks.assert_not_called()


@pytest.mark.django_db
def test_lineup_fragment_escapes_names_and_calculates_multipliers(manager, api):
    import_rows(manager.team, api)
    manager.team.squad_histories.filter(position=1).update(
        player_name='<img src=x onerror="alert(1)">'
    )
    client = Client()
    client.force_login(manager)
    response = client.get(reverse("rivals:teams.lineup", args=[manager.team_id]))
    assert response.status_code == 200
    html = response.content.decode()
    expected = sum(
        row.gameweek_points * row.multiplier
        for row in manager.team.squad_histories.all()
    )
    assert f"Squad points: {expected}" in html
    assert "0 pts · ×3" in html
    assert "&lt;img" in html and "<img src=x" not in html
    assert "season" in html and "gameweek" in html
    assert "101 pts" not in html
    assert html.count('aria-label="Club: Club"') == 15
    assert html.count(">CLB</p>") == 15


@pytest.mark.django_db
def test_existing_current_squad_gains_clubs_without_replacing_scores(manager, api):
    import_rows(manager.team, api)
    manager.team.squad_histories.update(club_name="", club_short_name="")
    rows, error = SquadHistoryService(manager.team, api=api).load("2026/27", 10)
    assert error is None
    assert all(row.club_name == "Club" and row.club_short_name == "CLB" for row in rows)
    assert rows[0].gameweek_points == 0 and rows[0].multiplier == 3
    assert manager.team.squad_histories.filter(club_short_name="CLB").count() == 15
    api.fetch_team_picks.assert_not_called()
    api.fetch_gameweek_live.assert_not_called()


@pytest.mark.django_db
def test_old_squad_does_not_use_live_clubs_when_club_was_not_saved(manager, api):
    import_rows(manager.team, api, season="2025/26")
    manager.team.squad_histories.update(club_name="", club_short_name="")
    rows, error = SquadHistoryService(manager.team, api=api).load("2025/26", 10)
    assert error is None and all(row.club_short_name == "" for row in rows)
    assert not api.method_calls


@pytest.mark.django_db
@pytest.mark.parametrize("formation", [(3, 5, 2), (4, 4, 2), (5, 4, 1)])
def test_historical_pitch_groups_starters_and_preserves_bench_order(
    manager, api, formation
):
    import_rows(manager.team, api, season="2025/26")
    types = [1] + [2] * formation[0] + [3] * formation[1] + [4] * formation[2]
    for position, kind in enumerate(types, 1):
        manager.team.squad_histories.filter(position=position).update(element_type=kind)
    client = Client()
    client.force_login(manager)
    response = client.get(
        reverse("rivals:teams.lineup", args=[manager.team_id]),
        {"season": "2025/26", "gameweek": 10},
    )
    assert response.status_code == 200
    assert [len(row["players"]) for row in response.context["pitch_rows"]] == [
        1,
        *formation,
    ]
    assert [p.position for p in response.context["bench"]] == [12, 13, 14, 15]
    html = response.content.decode()
    assert (
        f'aria-label="Starting XI, formation {"-".join(map(str, formation))}"' in html
    )
    assert html.count('class="lineup-player-name"') == 15
    assert 'aria-label="Captain"' in html and 'aria-label="Vice captain"' in html
    assert not api.method_calls


@pytest.mark.django_db
def test_lineup_outage_and_invalid_selection_offer_retry_without_exception_details(
    manager, api
):
    api.fetch_bootstrap_static.side_effect = RuntimeError("private diagnostic")
    client = Client()
    client.force_login(manager)
    with patch("rivals.services.squad_history_service.FplApiService", return_value=api):
        response = client.get(reverse("rivals:teams.lineup", args=[manager.team_id]))
        invalid = client.get(
            reverse("rivals:teams.lineup", args=[manager.team_id]), {"gameweek": "999"}
        )
    assert "Please try again" in response.content.decode()
    assert "View lineup" in response.content.decode()
    assert "private diagnostic" not in response.content.decode()
    assert "Choose an available gameweek" in invalid.content.decode()


@pytest.mark.django_db
def test_json_squad_endpoints_are_season_scoped(manager, api):
    manager.is_verified = True
    manager.save(update_fields=["is_verified"])
    import_rows(manager.team, api)
    import_rows(manager.team, api, season="2025/26")
    client = Client()
    client.force_login(manager)
    response = client.get(
        reverse("rivals:teams.squad-history", args=[manager.team_id, 10]),
        {"season": "2025/26"},
    )
    assert len(response.json()) == 15
    assert response.json()[0]["gameweek_points"] == 0
    response = client.get(
        reverse("api:team_squad", args=[manager.team_id]),
        {"season": "2025/26", "gameweek": 10},
    )
    assert response.json()["season"] == "2025/26"
    assert len(response.json()["squad"]) == 15
    assert (
        client.get(
            reverse("api:team_squad", args=[manager.team_id]), {"gameweek": "bad"}
        ).status_code
        == 400
    )


@pytest.mark.django_db
def test_cold_and_warm_analysis_only_load_data_on_cache_miss(manager, api):
    start = perf_counter()
    result = TeamAnalysisService(api=api).analyze_team(
        101, include_team_obj=manager.team
    )
    cold = perf_counter() - start
    assert "error" not in result
    assert result["current_gameweek"] == 10 and result["decision_gameweek"] == 11
    assert result["free_transfers"] is None
    assert "Enter your available free transfers" in " ".join(result["budget_insights"])
    assert len(api.method_calls) == 3
    api.reset_mock()
    start = perf_counter()
    assert (
        TeamAnalysisService(api=api).analyze_team(101, include_team_obj=manager.team)
        == result
    )
    warm = perf_counter() - start
    assert not api.method_calls
    print(
        f"Analysis cold: {cold * 1000:.1f} ms (3 mocked upstream calls); warm: {warm * 1000:.1f} ms (0 calls)"
    )


@pytest.mark.django_db
def test_analysis_reuses_saved_squad_instead_of_fetching_picks(manager, api):
    import_rows(manager.team, api)
    result = TeamAnalysisService(api=api).analyze_team(
        101, include_team_obj=manager.team
    )
    assert "error" not in result
    api.fetch_team_picks.assert_not_called()
    api.fetch_team_basic_info.assert_not_called()


@pytest.mark.django_db
def test_analysis_never_uses_legacy_latest_cache(manager, api):
    cache.set("tactical_analysis_101_latest", {"current_gameweek": 9, "wrong": True})
    service = TeamAnalysisService(api=api)
    assert service.get_cached_analysis(101) is None
    assert (
        service.analyze_team(101, include_team_obj=manager.team)["current_gameweek"]
        == 10
    )


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["players", "profile:1", "history:1", "transfers:1"])
def test_analysis_cache_invalidates_on_successful_input_refresh(manager, api, kind):
    kind = kind.replace(":1", f":{manager.team_id}")
    service = TeamAnalysisService(api=api)
    assert "error" not in service.analyze_team(101, include_team_obj=manager.team)
    assert service.get_cached_analysis(101)
    SyncState.objects.create(key=kind, kind=kind.split(":")[0], last_success_at=NOW)
    assert service.get_cached_analysis(101) is None


@pytest.mark.django_db
def test_analysis_cache_invalidates_after_squad_import_and_at_rollover(manager, api):
    service = TeamAnalysisService(api=api)
    service.analyze_team(101, include_team_obj=manager.team)
    import_rows(manager.team, api)
    assert service.get_cached_analysis(101) is None
    service.analyze_team(101, include_team_obj=manager.team)
    events = [dict(event) for event in EVENTS]
    events[2]["deadline_time"] = "2026-11-10T11:00:00Z"
    events.append({"id": 12, "deadline_time": "2026-11-21T12:00:00Z"})
    SyncState.objects.filter(key="context").update(
        payload={"events": events, "fixtures": []}
    )
    assert service.get_cached_analysis(101) is None


@pytest.mark.django_db
def test_analysis_loading_failure_is_plain_language_and_not_cached(manager, api):
    api.fetch_bootstrap_static.side_effect = RuntimeError("private diagnostic")
    service = TeamAnalysisService(api=api)
    result = service.analyze_team(101, include_team_obj=manager.team)
    assert "Please try again" in result["error"]
    assert "private" not in result["error"]
    assert service.get_cached_analysis(101) is None


@pytest.mark.django_db
def test_analysis_filters_unscheduled_and_current_gameweek_fixtures(manager, api):
    service = TeamAnalysisService(api=api)
    service.ensure_data()
    service.fixtures.append(
        {
            "event": 10,
            "team_h": 1,
            "team_a": 2,
            "team_h_difficulty": 5,
            "finished": False,
        }
    )
    assert service.calculate_fixture_difficulty(1) == pytest.approx(1.8)


@pytest.mark.django_db
def test_transfer_costs_are_never_used_as_free_transfer_counts(manager, api):
    api.fetch_team_basic_info.return_value["entry_history"] = {
        "event_transfers_cost": 8
    }
    result = TeamAnalysisService(api=api).quick_compare(101, 102)
    assert result["transfers"] == {"mine_free": None, "rival_free": None}
    api.fetch_team_basic_info.assert_not_called()


@pytest.mark.django_db
def test_planning_input_is_private_validated_and_scoped_to_next_deadline(manager, api):
    client = Client()
    client.force_login(manager)
    url = reverse("rivals:teams.analysis", args=[manager.team_id])
    with patch("rivals.services.team_analysis_service.FplApiService", return_value=api):
        response = client.post(
            url, {"free_transfers": 0, "season": "2026/27", "gameweek": "11"}
        )
        assert response.status_code == 200
        saved = TeamPlanningInput.objects.get(user=manager)
        assert (saved.season, saved.gameweek, saved.free_transfers) == (
            "2026/27",
            11,
            0,
        )
        assert "You entered 0 free transfers for GW11" in response.content.decode()
        assert (
            "between 0 and 5"
            in client.post(url, {"free_transfers": 8}).content.decode()
        )
        assert TeamPlanningInput.objects.get(user=manager).free_transfers == 0
        assert (
            client.post(
                url, {"free_transfers": "", "season": "2026/27", "gameweek": "11"}
            ).status_code
            == 200
        )
        assert TeamPlanningInput.objects.get(user=manager).free_transfers is None
    other = get_user_model().objects.create_user(
        username="other", email="other@example.com", password="test123456"
    )
    client.force_login(other)
    assert client.post(url, {"free_transfers": 3}).status_code == 403
    assert client.get(url).status_code == 403


@pytest.mark.django_db
def test_confirmed_transfers_do_not_carry_to_another_gameweek(manager, api):
    from django.test import RequestFactory

    from rivals.views.team_views import planning_input

    TeamPlanningInput.objects.create(
        user=manager, team=manager.team, season="2026/27", gameweek=10, free_transfers=5
    )
    request = RequestFactory().get("/")
    request.user = manager
    _, value = planning_input(request, manager.team)
    assert value is None


@pytest.mark.django_db
def test_own_squad_initial_render_has_no_upstream_requests_and_finite_panel_loads(
    manager,
):
    client = Client()
    client.force_login(manager)
    start = perf_counter()
    response = client.get(reverse("rivals:teams.show", args=[manager.team_id]))
    assert response.status_code == 200
    html = response.content.decode()
    assert "hx-request='{\"timeout\":18000}'" in html
    assert "js/team-panels.js" in html
    assert "Plan transfers" in html and "decisions/summary/" in html
    assert "let availableGameweeks" not in html
    assert "data-panel-status" in html

    print(
        f"Initial squad page: {(perf_counter() - start) * 1000:.1f} ms; upstream calls: 0"
    )


@pytest.mark.django_db
def test_completed_week_is_refreshed_once_before_becoming_immutable(manager, api):
    import_rows(manager.team, api, gameweek=1)
    manager.team.squad_histories.filter(gameweek=1).update(
        updated_at=NOW - timedelta(days=2)
    )
    api.fetch_bootstrap_static.return_value["events"][0]["data_checked"] = True
    api.fetch_gameweek_live.return_value["elements"][0]["stats"]["total_points"] = 7
    service = SquadHistoryService(manager.team, api=api)
    rows, error = service.load("2026/27", 1)
    assert error is None and rows[0].gameweek_points == 7
    assert all(row.points_final for row in rows)
    api.reset_mock()
    manager.team.squad_histories.update(updated_at=NOW - timedelta(days=2))
    service.load("2026/27", 1)
    assert not api.method_calls


@pytest.mark.django_db
def test_partial_live_scores_cannot_finalize_old_provisional_points(manager, api):
    import_rows(manager.team, api, gameweek=1)
    manager.team.squad_histories.update(updated_at=NOW - timedelta(days=2))
    api.fetch_bootstrap_static.return_value["events"][0]["data_checked"] = True
    api.fetch_gameweek_live.return_value["elements"] = []
    rows, error = SquadHistoryService(manager.team, api=api).load("2026/27", 1)
    assert "out of date" in error
    assert all(not row.points_final for row in rows)
    assert all(row.updated_at < NOW for row in rows)


@pytest.mark.django_db
def test_preseason_and_unknown_weeks_do_not_start_http_loading(manager, api):
    SyncState.objects.filter(key="context").update(
        payload={
            "events": [{"id": 1, "deadline_time": "2027-08-15T12:00:00Z"}],
            "fixtures": [],
        }
    )
    service = SquadHistoryService(manager.team, api=api)
    selected = service.selection()
    assert selected["gameweek"] is None
    assert service.load(selected["season"], selected["gameweek"]) == ([], None)
    assert not api.method_calls


@pytest.mark.django_db
def test_planning_form_requires_csrf(manager):
    client = Client(enforce_csrf_checks=True)
    client.force_login(manager)
    response = client.post(
        reverse("rivals:teams.analysis", args=[manager.team_id]), {"free_transfers": 2}
    )
    assert response.status_code == 403
    assert not TeamPlanningInput.objects.exists()


@pytest.mark.django_db
def test_analysis_does_not_recommend_captaining_a_blank_gameweek(manager, api):
    service = TeamAnalysisService(api=api)
    service.ensure_data()
    service.fixtures[0]["event"] = 12
    assert (
        service.get_captain_suggestions(api.fetch_team_picks.return_value["picks"])
        == []
    )


@pytest.mark.django_db
def test_representative_player_pool_produces_alternatives_without_squad_duplicates(
    manager, api
):
    base = api.fetch_bootstrap_static.return_value["elements"][0]
    api.fetch_bootstrap_static.return_value["elements"].extend(
        {
            **base,
            "id": element,
            "element_type": 1 + element % 4,
            "team": 2,
            "first_name": "Alternative",
            "second_name": str(element),
            "form": "8.0",
            "points_per_game": "7.0",
        }
        for element in range(16, 801)
    )
    start = perf_counter()
    result = TeamAnalysisService(api=api).analyze_team(
        101, include_team_obj=manager.team
    )
    elapsed = perf_counter() - start
    assert "error" not in result and result["transfer_recommendations"]
    assert all(
        alt["id"] > 15
        for rec in result["transfer_recommendations"]
        for alt in rec["transfer_in"]
    )
    print(
        f"800-player cold analysis: {elapsed * 1000:.1f} ms (3 mocked upstream calls)"
    )


@pytest.mark.django_db
def test_form_left_open_across_deadline_cannot_confirm_old_transfer_count(manager):
    client = Client()
    client.force_login(manager)
    response = client.post(
        reverse("rivals:teams.analysis", args=[manager.team_id]),
        {"free_transfers": 5, "season": "2026/27", "gameweek": "10"},
    )
    assert "Reload your squad" in response.content.decode()
    assert not TeamPlanningInput.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("context_status", ["missing", "queued", "stale"])
def test_lineup_backfills_existing_points_without_a_recent_dispatcher(
    manager, api, context_status
):
    import_rows(manager.team, api, points=False)
    if context_status == "missing":
        SyncState.objects.filter(key="context").delete()
    elif context_status == "queued":
        SyncState.objects.filter(key="context").update(
            status="queued", last_success_at=None, payload={}
        )
    else:
        SyncState.objects.filter(key="context").update(
            last_success_at=NOW - timedelta(hours=1)
        )
    client = Client()
    client.force_login(manager)
    with patch("rivals.services.squad_history_service.FplApiService", return_value=api):
        response = client.get(reverse("rivals:teams.lineup", args=[manager.team_id]))
    assert response.status_code == 200
    assert "0 pts · ×3" in response.content.decode()
    assert "Squad points:" in response.content.decode()
    assert (
        manager.team.squad_histories.filter(gameweek_points__isnull=True).count() == 0
    )
    api.fetch_gameweek_live.assert_called_once_with(10, "2026/27")


@pytest.mark.django_db
def test_unknown_context_cannot_import_current_scores_into_a_previous_season(
    manager, api
):
    import_rows(manager.team, api, season="2025/26", points=False)
    manager.team.current_season = "2025/26"
    manager.team.save(update_fields=["current_season"])
    SyncState.objects.filter(key="context").delete()
    rows, error = SquadHistoryService(manager.team, api=api).load("2025/26", 10)
    assert len(rows) == 15 and "unavailable for this season" in error
    assert all(row.gameweek_points is None for row in rows)
    api.fetch_team_picks.assert_not_called()
    api.fetch_gameweek_live.assert_not_called()
