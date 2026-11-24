from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from rivals.models import (
    AchievementAward,
    AchievementDismissal,
    AchievementEvent,
    AchievementSeason,
    GameweekData,
    LeagueAchievementResult,
    MiniLeague,
    SquadHistory,
    SyncState,
    Team,
    UserMiniLeague,
)
from rivals.services.achievement_service import (
    cabinet,
    dashboard_achievements,
    evaluate_team,
    publish_events,
    queue_missing_squads,
    record_league_results,
)

pytestmark = pytest.mark.django_db
SEASON = "2026/27"


@pytest.fixture
def team():
    return Team.objects.create(
        fpl_team_id="123",
        team_name="Trophy Hunters",
        current_season=SEASON,
        started_event=1,
    )


@pytest.fixture
def user(team):
    return get_user_model().objects.create_user(
        username="hunter",
        email="hunter@example.com",
        password="test123456",
        team=team,
        is_verified=True,
        fpl_id=team.fpl_team_id,
    )


def event(week, **kwargs):
    return AchievementEvent.objects.create(
        season=SEASON,
        gameweek=week,
        **{
            "finished": True,
            "data_checked": True,
            "average_score": 50,
            "finalised_at": timezone.now() - timedelta(hours=2),
            "is_last": week == 38,
            **kwargs,
        },
    )


def history(team, week, **kwargs):
    return GameweekData.objects.create(
        team=team,
        season=SEASON,
        gameweek=week,
        **{"points": 100, "overall_rank": 100000 - week * 100, **kwargs},
    )


def squad(team, week, points=12, multiplier=2, inherited=False):
    rows = []
    for index in range(1, 16):
        effective = 2 if inherited else 1
        rows.append(
            SquadHistory(
                team=team,
                season=SEASON,
                gameweek=week,
                player_fpl_id=index,
                position=index,
                multiplier=(
                    multiplier if index == effective else (1 if index <= 11 else 0)
                ),
                is_captain=index == 1,
                is_vice_captain=index == 2,
                element_type=3,
                player_name=f"Player {index}",
                player_cost=50,
                points_final=True,
                gameweek_points=points if index == effective else 2,
                gameweek_minutes=90,
            )
        )
    if inherited:
        rows[0].multiplier = 0
        rows[0].gameweek_minutes = 0
    SquadHistory.objects.bulk_create(rows)


def active(team, code):
    return AchievementAward.objects.filter(team=team, code=code, active=True)


@pytest.mark.parametrize(
    "points,tiers",
    [
        (99, []),
        (100, [100]),
        (124, [100]),
        (125, [100, 125]),
        (149, [100, 125]),
        (150, [100, 125, 150]),
    ],
)
def test_score_thresholds_use_gross_points_and_allow_chips(team, points, tiers):
    event(1)
    history(team, 1, points=points, event_transfers_cost=12, chip_name="bboost")
    evaluate_team(team, SEASON)
    assert list(active(team, "centurion").values_list("tier", flat=True)) == tiers
    evaluate_team(team, SEASON)
    assert active(team, "centurion").count() == len(tiers)
    if tiers:
        evidence = active(team, "centurion").first().evidence
        assert evidence["hits"] == 12
        assert evidence["chip"] == "bboost"


@pytest.mark.parametrize(
    "points,multiplier,inherited,tiers",
    [
        (9, 3, False, []),
        (10, 2, False, [10]),
        (15, 3, True, [10, 15]),
        (20, 2, False, [10, 15, 20]),
    ],
)
def test_captain_uses_raw_points_and_actual_inheritance(
    team, points, multiplier, inherited, tiers
):
    event(1)
    history(team, 1)
    squad(team, 1, points, multiplier, inherited)
    evaluate_team(team, SEASON)
    assert (
        list(active(team, "captain_fantastic").values_list("tier", flat=True)) == tiers
    )
    if tiers:
        evidence = active(team, "captain_fantastic").first().evidence
        assert evidence["contribution"] == points * multiplier
        assert evidence["player_id"] == (2 if inherited else 1)


def test_streaks_use_overall_rank_not_gameweek_rank_and_break_on_ties(team):
    for week, rank in enumerate([1000, 900, 800, 700, 700, 600], 1):
        event(week)
        history(
            team,
            week,
            overall_rank=rank,
            rank=week * 100,
            points=51 if week != 6 else 50,
        )
    result = evaluate_team(team, SEASON)
    assert list(active(team, "green_machine").values_list("tier", "gameweek")) == [
        (3, 4)
    ]
    assert result["progress"]["green_machine"] == {
        "current": 1,
        "longest": 3,
        "complete": True,
    }
    assert list(active(team, "on_a_roll").values_list("tier", "gameweek")) == [
        (3, 3),
        (5, 5),
    ]
    assert result["progress"]["on_a_roll"]["current"] == 0


def test_gap_pauses_awards_and_recovers_without_bridging(team):
    for week in range(1, 5):
        event(week)
        if week != 2:
            history(team, week)
    result = evaluate_team(team, SEASON)
    assert not active(team, "green_machine").exists()
    assert not active(team, "on_a_roll").exists()
    assert result["progress"]["on_a_roll"]["complete"] is False
    history(team, 2)
    result = evaluate_team(team, SEASON)
    assert active(team, "green_machine").get().gameweek == 4
    assert active(team, "on_a_roll").get().gameweek == 3


def test_late_start_is_a_baseline_and_seasons_do_not_share_streaks(team):
    team.started_event = 10
    team.save()
    for week in range(10, 14):
        event(week)
        history(team, week)
    evaluate_team(team, SEASON)
    assert active(team, "green_machine").get().gameweek == 13
    evaluate_team(team, "2027/28")
    assert not AchievementAward.objects.filter(team=team, season="2027/28").exists()


def test_corrected_scores_retract_dependent_awards_and_reactivate_same_occurrence(team):
    for week in range(1, 4):
        event(week)
        history(team, week)
    evaluate_team(team, SEASON)
    streak = active(team, "on_a_roll").get()
    GameweekData.objects.filter(team=team, gameweek=2).update(
        points=49, updated_at=timezone.now()
    )
    evaluate_team(team, SEASON)
    assert not active(team, "on_a_roll").exists()
    assert not active(team, "centurion").filter(gameweek=2).exists()
    GameweekData.objects.filter(team=team, gameweek=2).update(
        points=100, updated_at=timezone.now()
    )
    evaluate_team(team, SEASON)
    assert active(team, "on_a_roll").get().pk == streak.pk


def test_unchecked_or_old_sources_never_unlock_and_unfinalisation_retracts(team):
    e = event(1, data_checked=False, finalised_at=None)
    row = history(team, 1)
    evaluate_team(team, SEASON)
    assert not team.achievements.exists()
    e.data_checked = True
    e.finalised_at = timezone.now()
    e.save()
    evaluate_team(team, SEASON)
    assert not active(team, "centurion").exists()
    row.save()
    evaluate_team(team, SEASON)
    assert active(team, "centurion").exists()
    e.finished = False
    e.finalised_at = None
    e.save()
    evaluate_team(team, SEASON)
    assert not active(team, "centurion").exists()


def test_missing_captain_data_does_not_prevent_scoring_awards(team):
    event(1)
    history(team, 1)
    result = evaluate_team(team, SEASON)
    assert active(team, "centurion").exists()
    assert result["missing_squads"] == [1]
    squad(team, 1)
    SquadHistory.objects.filter(team=team, position=15).update(points_final=False)
    evaluate_team(team, SEASON)
    assert not active(team, "captain_fantastic").exists()


def test_backfill_is_quiet_including_late_lineups_but_new_weeks_announce(team, user):
    event(1)
    history(team, 1)
    evaluate_team(team, SEASON)
    squad(team, 1)
    evaluate_team(team, SEASON)
    assert not team.achievements.filter(announce=True).exists()
    event(2)
    history(team, 2, points=150)
    evaluate_team(team, SEASON)
    announcements = dashboard_achievements(user, SEASON)["achievement_announcements"]
    assert len(announcements) == 1
    assert announcements[0]["tier"] == 150
    card = next(
        c
        for c in cabinet(user, team, SEASON)["achievement_cards"]
        if c["code"] == "centurion"
    )
    assert card["count"] == 2
    assert card["best"] == 150


@pytest.mark.parametrize(
    "league_type,scoring,count,eligible",
    [
        ("x", "c", 2, True),
        ("s", "c", 100, False),
        ("x", "h", 4, False),
        ("x", "c", 1, False),
    ],
)
def test_league_eligibility_and_season_end(team, league_type, scoring, count, eligible):
    event(37)
    event(38)
    league = MiniLeague.objects.create(
        fpl_league_id="8", name="Friends", league_type=league_type, scoring_type=scoring
    )
    for week in (37, 38):
        record_league_results(
            league,
            SEASON,
            week,
            [{"entry": 123, "rank": 1}],
            entrant_count=count,
            fetched_at=timezone.now(),
        )
    evaluate_team(team, SEASON)
    assert active(team, "top_of_table").count() == int(eligible)
    assert active(team, "league_champion").count() == int(eligible)
    if eligible:
        assert active(team, "top_of_table").get().gameweek == 37
        assert active(team, "league_champion").get().gameweek == 38
        record_league_results(
            league,
            SEASON,
            38,
            [{"entry": 123, "rank": 2}],
            entrant_count=count,
            fetched_at=timezone.now(),
        )
        evaluate_team(team, SEASON)
        assert active(team, "top_of_table").exists()
        assert not active(team, "league_champion").exists()


def test_old_or_unfinished_league_results_are_not_certified(team):
    e = event(1)
    league = MiniLeague.objects.create(
        fpl_league_id="8", name="Friends", league_type="x", scoring_type="c"
    )
    record_league_results(
        league,
        SEASON,
        1,
        [{"entry": 123, "rank": 1}],
        entrant_count=2,
        fetched_at=e.finalised_at - timedelta(seconds=1),
    )
    assert not LeagueAchievementResult.objects.exists()


def test_tied_winners_each_get_trophies(team):
    event(38)
    other = Team.objects.create(fpl_team_id="456", team_name="Other")
    league = MiniLeague.objects.create(
        fpl_league_id="8", name="Friends", league_type="x", scoring_type="c"
    )
    record_league_results(
        league,
        SEASON,
        38,
        [{"entry": 123, "rank": 1}, {"entry": 456, "rank": 1}],
        entrant_count=2,
        fetched_at=timezone.now(),
    )
    for owner in (team, other):
        evaluate_team(owner, SEASON)
        assert active(owner, "league_champion").count() == 1


def test_cabinet_and_dismissal_preserve_league_and_account_visibility(
    client, team, user
):
    event(38)
    history(team, 38)
    league = MiniLeague.objects.create(
        fpl_league_id="8", name="Secret League", league_type="x", scoring_type="c"
    )
    record_league_results(
        league,
        SEASON,
        38,
        [{"entry": 123, "rank": 1}],
        entrant_count=2,
        fetched_at=timezone.now(),
    )
    evaluate_team(team, SEASON)
    own = active(team, "centurion").get()
    trophy = active(team, "league_champion").get()
    client.force_login(user)
    with patch("requests.get", side_effect=AssertionError("HTTP during page render")):
        response = client.get(reverse("rivals:teams.achievements", args=[team.pk]))
    assert response.status_code == 200
    assert b"Captain Fantastic" in response.content
    assert b"Secret League" not in response.content
    assert (
        client.get(
            reverse("rivals:teams.achievements", args=[team.pk]), {"season": "nope"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            reverse("rivals:achievements.dismiss"), {"award_id": trophy.pk}
        ).status_code
        == 400
    )
    assert (
        client.post(
            reverse("rivals:achievements.dismiss"), {"award_id": own.pk}
        ).status_code
        == 302
    )
    assert AchievementDismissal.objects.filter(user=user, award=own).exists()
    UserMiniLeague.objects.create(user=user, mini_league=league, active=True)
    response = client.get(reverse("rivals:teams.achievements", args=[team.pk]))
    assert b"Secret League" in response.content
    other = get_user_model().objects.create_user(
        username="other",
        email="other@example.com",
        password="test123456",
        team=team,
        is_verified=True,
        fpl_id=team.fpl_team_id,
    )
    assert not AchievementDismissal.objects.filter(user=other).exists()
    client.force_login(other)
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(user)
    assert (
        csrf_client.post(
            reverse("rivals:achievements.dismiss"), {"award_id": own.pk}
        ).status_code
        == 403
    )
    client.logout()
    assert (
        client.get(reverse("rivals:teams.achievements", args=[team.pk])).status_code
        == 302
    )


def test_dismissal_rejects_another_teams_award(client, team, user):
    other = Team.objects.create(fpl_team_id="456", team_name="Other")
    award = AchievementAward.objects.create(
        team=other, season=SEASON, code="centurion", tier=100, gameweek=1
    )
    client.force_login(user)
    url = reverse("rivals:achievements.dismiss")
    assert client.post(url, {"award_id": award.pk}).status_code == 400
    assert client.get(url).status_code == 405
    assert not AchievementDismissal.objects.exists()


def test_award_uniqueness_includes_no_league(team):
    event(1)
    history(team, 1)
    evaluate_team(team, SEASON)
    with pytest.raises(IntegrityError), transaction.atomic():
        AchievementAward.objects.create(
            team=team, season=SEASON, code="centurion", tier=100, gameweek=1
        )


def test_event_publication_preserves_first_final_check_and_does_not_end_season_early():
    source = [
        {"id": 10, "finished": True, "data_checked": True, "average_entry_score": 55}
    ]
    publish_events(SEASON, source)
    e = AchievementEvent.objects.get()
    assert not e.is_last
    first_check = e.finalised_at
    publish_events(SEASON, source)
    e.refresh_from_db()
    assert e.finalised_at == first_check
    source[0]["data_checked"] = False
    publish_events(SEASON, source)
    e.refresh_from_db()
    assert e.finalised_at is None


def test_missing_squad_backfill_is_bounded_and_rechecks_final_data(team):
    for week in range(1, 5):
        event(week)
        history(team, week)
    evaluate_team(team, SEASON)
    with patch(
        "rivals.services.fpl_snapshot_service.enqueue_squad", return_value=True
    ) as enqueue:
        queue_missing_squads(team, SEASON)
    assert enqueue.call_count == 2
    assert [c.args[2] for c in enqueue.call_args_list] == [1, 2]
    for week in range(1, 5):
        squad(team, week)
    SquadHistory.objects.update(updated_at=timezone.now() - timedelta(days=2))
    AchievementEvent.objects.update(finalised_at=timezone.now() - timedelta(days=3))
    with patch(
        "rivals.services.fpl_snapshot_service.enqueue_squad", return_value=True
    ) as enqueue:
        queue_missing_squads(team, SEASON)
    assert enqueue.call_count == 2
    assert all(c.kwargs["force"] for c in enqueue.call_args_list)


def test_evaluation_runs_as_a_worker_refresh_without_http(team):
    from rivals.tasks import refresh_resource
    from rivals.services.gameweek_context_service import GameweekContextService

    event(1)
    history(team, 1)
    SyncState.objects.create(
        key=f"achievements:{team.pk}",
        kind="achievements",
        object_id=team.pk,
        generation=1,
        status="queued",
    )
    context = {"season": SEASON, "stale": False, "scoring_gameweek": 1}
    with (
        patch.object(GameweekContextService, "cached", return_value=context),
        patch("requests.get", side_effect=AssertionError("HTTP during evaluation")),
    ):
        refresh_resource.now(f"achievements:{team.pk}", 1)
    state = SyncState.objects.get(key=f"achievements:{team.pk}")
    assert state.status == "ok"
    assert state.payload["evaluated"] == 1
    assert active(team, "centurion").exists()


def test_final_captain_correction_retracts_badge(team):
    event(1)
    history(team, 1)
    squad(team, 1)
    evaluate_team(team, SEASON)
    assert active(team, "captain_fantastic").exists()
    SquadHistory.objects.filter(team=team, position=1).update(
        gameweek_points=9, updated_at=timezone.now()
    )
    evaluate_team(team, SEASON)
    assert not active(team, "captain_fantastic").exists()


def test_checked_squad_refresh_replaces_final_scores_but_provisional_does_not(team):
    from unittest.mock import Mock
    from rivals.services.fpl_api_service import FplApiService
    from rivals.services.squad_history_service import SquadHistoryService
    from rivals.services.team_sync_service import TeamSyncService

    event(1)
    history(team, 1)
    squad(team, 1)
    evaluate_team(team, SEASON)
    assert active(team, "captain_fantastic").exists()
    picks = [
        {
            "element": index,
            "position": index,
            "multiplier": 2 if index == 1 else 1 if index <= 11 else 0,
            "is_captain": index == 1,
            "is_vice_captain": index == 2,
        }
        for index in range(1, 16)
    ]
    elements = [
        {"id": index, "element_type": 3, "web_name": f"Renamed {index}", "now_cost": 50}
        for index in range(1, 16)
    ]
    api = Mock(spec=FplApiService)
    api.fetch_bootstrap_static.return_value = {
        "events": [
            {
                "id": 1,
                "deadline_time": "2026-08-15T12:00:00Z",
                "finished": True,
                "data_checked": True,
            }
        ],
        "elements": elements,
    }
    api.fetch_team_picks.return_value = {"picks": picks}
    api.fetch_gameweek_live.return_value = {
        "elements": [
            {
                "id": index,
                "stats": {"total_points": 9 if index == 1 else 2, "minutes": 90},
            }
            for index in range(1, 16)
        ]
    }
    service = SquadHistoryService(team, api)
    service.context = {"season": SEASON, "stale": False, "scoring_gameweek": 1}
    rows, notice = service.load(SEASON, 1, refresh_final=True)
    assert notice is None
    assert rows[0].gameweek_points == 9
    assert rows[0].player_name == "Player 1"
    api.fetch_gameweek_live.assert_called_once_with(1, SEASON, force_refresh=True)
    evaluate_team(team, SEASON)
    assert not active(team, "captain_fantastic").exists()
    sync = TeamSyncService(team)
    checked_at = rows[0].updated_at
    api.fetch_bootstrap_static.return_value["events"][0]["data_checked"] = False
    api.fetch_gameweek_live.return_value["elements"][0]["stats"]["total_points"] = 99
    rows, notice = service.load(SEASON, 1, refresh_final=True)
    assert notice == "Final results are awaiting verification."
    assert rows[0].gameweek_points == 9
    assert rows[0].updated_at == checked_at
    assert sync.sync_squad_for_gameweek(
        1,
        picks={"picks": picks},
        elements={p["id"]: p for p in elements},
        points={p["id"]: 99 for p in elements},
        points_final=False,
        refresh=True,
    )
    assert (
        SquadHistory.objects.get(team=team, gameweek=1, position=1).gameweek_points == 9
    )


def test_full_standings_correction_removes_a_departed_winner(team):
    event(38)
    league = MiniLeague.objects.create(
        fpl_league_id="8", name="Friends", league_type="x", scoring_type="c"
    )
    record_league_results(
        league,
        SEASON,
        38,
        [{"entry": 123, "rank": 1}],
        entrant_count=2,
        fetched_at=timezone.now(),
    )
    evaluate_team(team, SEASON)
    assert active(team, "league_champion").exists()
    record_league_results(
        league,
        SEASON,
        38,
        [
            {
                "entry": 456,
                "entry_name": "New winner",
                "player_name": "Manager",
                "rank": 1,
            },
            {"entry": 789, "entry_name": "Second", "rank": 2},
        ],
        entrant_count=2,
        fetched_at=timezone.now(),
        leaders_complete=True,
    )
    evaluate_team(team, SEASON)
    assert not active(team, "league_champion").exists()
    other = Team.objects.get(fpl_team_id="456")
    evaluate_team(other, SEASON)
    assert active(other, "league_champion").exists()
    assert not Team.objects.filter(fpl_team_id="789").exists()


def test_checked_context_and_history_publications_queue_real_evaluation(
    team, django_capture_on_commit_callbacks
):
    from unittest.mock import Mock
    from rivals.services.fpl_api_service import FplApiService
    from rivals.tasks import refresh_resource

    api = Mock(spec=FplApiService)
    api.snapshots = {}
    api.SEASON_CACHE_KEY = FplApiService.SEASON_CACHE_KEY
    api.SEASON_CACHE_TIMEOUT = FplApiService.SEASON_CACHE_TIMEOUT
    api.fetch_bootstrap_static.return_value = {
        "events": [
            {
                "id": 1,
                "deadline_time": "2026-08-15T12:00:00Z",
                "finished": True,
                "data_checked": True,
                "average_entry_score": 50,
            }
        ]
    }
    api.fetch_fixtures_data.return_value = []
    api.fetch_team_history.return_value = {
        "current": [
            {
                "event": 1,
                "points": 125,
                "total_points": 125,
                "rank": 1234567,
                "overall_rank": 2000,
                "event_transfers": 1,
                "event_transfers_cost": 4,
                "points_on_bench": 0,
                "bank": 0,
            }
        ],
        "past": [],
        "chips": [],
    }
    SyncState.objects.create(
        key="context", kind="context", generation=1, status="queued"
    )
    SyncState.objects.create(
        key=f"history:{team.pk}",
        kind="history",
        object_id=team.pk,
        generation=1,
        status="queued",
    )
    with patch("rivals.tasks.FplApiService", return_value=api):
        refresh_resource.now("context", 1)
        with django_capture_on_commit_callbacks(execute=True):
            refresh_resource.now(f"history:{team.pk}", 1)
    row = GameweekData.objects.get(team=team, gameweek=1)
    assert row.rank == 1234567 and row.overall_rank == 2000
    state = SyncState.objects.get(key=f"achievements:{team.pk}")
    assert state.status == "queued"
    with patch("requests.get", side_effect=AssertionError("Unexpected HTTP")):
        refresh_resource.now(state.key, state.generation)
    assert list(active(team, "centurion").values_list("tier", flat=True)) == [100, 125]


def test_dashboard_groups_and_dismisses_all_tiers_without_http(client, team, user):
    event(1)
    history(team, 1)
    evaluate_team(team, SEASON)
    event(2)
    history(team, 2, points=150)
    evaluate_team(team, SEASON)
    client.force_login(user)
    context = {
        "season": SEASON,
        "stale": False,
        "scoring_gameweek": 2,
        "decision_gameweek": None,
    }
    with (
        patch(
            "rivals.services.gameweek_context_service.GameweekContextService.cached",
            return_value=context,
        ),
        patch("requests.get", side_effect=AssertionError("Unexpected HTTP")),
    ):
        page = client.get(reverse("rivals:dashboard"))
    assert page.status_code == 200
    assert b"Earned this gameweek" in page.content
    announcements = dashboard_achievements(user, SEASON)["achievement_announcements"]
    client.post(
        reverse("rivals:achievements.dismiss"), {"award_id": announcements[0]["id"]}
    )
    assert AchievementDismissal.objects.filter(user=user).count() == 3
    assert not dashboard_achievements(user, SEASON)["achievement_announcements"]


def test_evidence_link_selects_the_awarded_gameweek(client, team, user):
    event(1)
    history(team, 1)
    event(2)
    history(team, 2)
    client.force_login(user)
    response = client.get(
        reverse("rivals:teams.show", args=[team.pk]), {"season": SEASON, "gameweek": 1}
    )
    assert response.status_code == 200
    assert response.context["squad_selection"]["gameweek"] == 1
    assert b"gameweek=1" in response.content


def test_archive_season_without_lineups_is_readable(client, team, user):
    AchievementSeason.objects.create(team=team, season="2025/26", baseline_gameweek=38)
    client.force_login(user)
    response = client.get(
        reverse("rivals:teams.achievements", args=[team.pk]), {"season": "2025/26"}
    )
    assert response.status_code == 200
    assert response.context["achievement_season"] == "2025/26"


def test_late_start_and_missing_baseline_cannot_award_a_green_streak(team):
    for week in range(1, 5):
        event(week)
        history(team, week, overall_rank=None if week == 1 else 1000 - week * 10)
    evaluate_team(team, SEASON)
    assert not active(team, "green_machine").exists()


def test_invalidated_dependency_retracts_streak_with_other_results_missing(team):
    for week in range(1, 5):
        event(week)
        history(team, week)
    evaluate_team(team, SEASON)
    assert active(team, "green_machine").exists()
    AchievementEvent.objects.filter(gameweek=2).update(
        finalised_at=None, data_checked=False
    )
    evaluate_team(team, SEASON)
    assert not active(team, "green_machine").exists()
    assert not active(team, "on_a_roll").exists()


@pytest.mark.parametrize("summary", [False, True])
def test_real_league_publication_records_verified_leadership(team, summary):
    from unittest.mock import Mock
    from rivals.services.fpl_api_service import FplApiService
    from rivals.services.league_import_service import LeagueImportService

    event(38)
    league = MiniLeague.objects.create(
        fpl_league_id="8", name="Friends", summary_only=summary
    )
    state = SyncState.objects.create(
        key=f"league:{league.pk}",
        kind="league",
        object_id=league.pk,
        generation=1,
        status="running",
        last_attempt_at=timezone.now(),
    )
    api = Mock(spec=FplApiService)
    api.fetch_mini_league_page.return_value = {
        "league": {"name": "Friends", "league_type": "x", "scoring": "c"},
        "standings": {
            "page": 1,
            "has_next": False,
            "results": [
                {
                    "entry": 123,
                    "entry_name": "Hunters",
                    "player_name": "Manager",
                    "rank": 1,
                    "total": 2500,
                },
                {
                    "entry": 456,
                    "entry_name": "Other",
                    "player_name": "Rival",
                    "rank": 2,
                    "total": 2400,
                },
            ],
        },
    }
    context = {"season": SEASON, "scoring_gameweek": 38, "stale": False}
    with patch(
        "rivals.services.gameweek_context_service.GameweekContextService.cached",
        return_value=context,
    ):
        assert LeagueImportService(state, api, context).run_batch()
    result = LeagueAchievementResult.objects.get(team=team)
    assert result.eligible and result.entrant_count == 2
    evaluate_team(team, SEASON)
    assert active(team, "league_champion").exists()


def test_dispatcher_refreshes_pre_final_sources_without_waiting_for_ttl(team, user):
    from rivals.tasks import refresh_tick
    from rivals.services.refresh_service import resource_key

    e = event(1)
    recent = timezone.now() - timedelta(minutes=1)
    e.finalised_at = timezone.now()
    e.save()
    for kind in ("history", "profile", "achievements"):
        SyncState.objects.create(
            key=resource_key(kind, team.pk),
            kind=kind,
            object_id=team.pk,
            last_success_at=recent,
            payload={"stamp": f"{SEASON}:1", "final_gameweek": 0},
        )
    context = {
        "season": SEASON,
        "scoring_gameweek": 1,
        "stale": False,
        "latest_checked_gameweek": 1,
        "refresh_seconds": 1800,
    }
    with (
        patch(
            "rivals.services.gameweek_context_service.GameweekContextService.cached",
            return_value=context,
        ),
        patch("rivals.services.deadline_digest_service.dispatch_digests"),
    ):
        refresh_tick.now()
    for kind in ("history", "profile", "achievements"):
        state = SyncState.objects.get(key=resource_key(kind, team.pk))
        assert state.status == "queued"


def test_evaluation_failure_preserves_awards_and_schedules_retry(team):
    from rivals.tasks import refresh_resource

    event(1)
    history(team, 1)
    evaluate_team(team, SEASON)
    key = f"achievements:{team.pk}"
    SyncState.objects.create(
        key=key, kind="achievements", object_id=team.pk, generation=1, status="queued"
    )
    with (
        patch(
            "rivals.services.gameweek_context_service.GameweekContextService.cached",
            return_value={"season": SEASON, "stale": False},
        ),
        patch(
            "rivals.services.achievement_service.evaluate_team",
            side_effect=RuntimeError("retry"),
        ),
    ):
        refresh_resource.now(key, 1)
    state = SyncState.objects.get(key=key)
    assert state.status == "error"
    assert state.next_attempt_at > timezone.now()
    assert active(team, "centurion").exists()
