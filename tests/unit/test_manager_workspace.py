from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.urls import reverse

from rivals.models import (
    DecisionRecord,
    FplSnapshot,
    GameweekData,
    LeagueRankSnapshot,
    MiniLeague,
    Team,
    TrackedTeam,
    UserMiniLeague,
    SeasonRankGoal,
    SyncState,
    TeamPlanningInput,
)
from rivals.services.decision_engine import DecisionEngine
from rivals.services.transfer_evidence import comparison
from rivals.services.weekly_briefing import player_review, explain_gap
from tests.unit.test_decision_engine import api, isolated, manager, NOW, form_data


def login(client, manager):
    manager.fpl_id = manager.team.fpl_team_id
    manager.is_verified = True
    manager.save(update_fields=["fpl_id", "is_verified"])
    client.force_login(manager)


def test_preview_save_roll_and_reselect(client, manager, api):
    login(client, manager)
    engine = DecisionEngine(manager, api).load()
    payload = dict(form_data(engine), out_1=8, in_1=18, out_2=3, in_2=17)
    with patch("rivals.services.decision_engine.FplApiService", return_value=api):
        preview = client.post(reverse("rivals:decisions.workspace"), payload)
        route = preview.context["outlook_route"]
        assert route["key"] == "3:17,8:18"
        assert not TeamPlanningInput.objects.get().active_decision_id
        for week, line in zip(
            preview.context["route_weeks"], route["lineups"], strict=True
        ):
            players = [p for row in week["rows"] for p in row["players"]] + week[
                "bench"
            ]
            assert {p["id"] for p in players if p["incoming"]} == {17, 18}
            assert {p["id"] for p in players} == set(route["ids"])
            assert week["points"] == round(line["points"], 1)
        assert "Save this plan" in preview.content.decode()
        confirm = client.post(
            reverse("rivals:decisions.save"), {"choice": route["save_token"]}
        )
        assert confirm.url == reverse("rivals:decisions")
        saved = TeamPlanningInput.objects.get().active_decision
        assert (
            client.get(reverse("rivals:decisions.preview")).context["outlook_route"][
                "key"
            ]
            == route["key"]
        )
        summary = client.get(reverse("rivals:decisions.summary"))
        assert summary.context["outlook_route"]["key"] == route["key"]
        hold = preview.context["report"]["baseline"]
        client.post(reverse("rivals:decisions.save"), {"choice": hold["save_token"]})
        assert (
            client.get(reverse("rivals:decisions.summary")).context["outlook_route"][
                "key"
            ]
            == "hold"
        )
        client.post(reverse("rivals:decisions.select", args=[saved.pk]))
        assert TeamPlanningInput.objects.get().active_decision_id == saved.pk
        assert DecisionRecord.objects.count() == 2


def test_target_and_weekly_review(client, manager, api):
    login(client, manager)
    season = "2026/27"
    api.fetch_bootstrap_static.return_value["events"].append(
        {
            "id": 9,
            "deadline_time": "2026-11-01T12:00:00Z",
            "finished": True,
            "data_checked": True,
        }
    )
    league = MiniLeague.objects.create(fpl_league_id="7", name="Office league")
    UserMiniLeague.objects.create(user=manager, mini_league=league)
    LeagueRankSnapshot.objects.create(
        team=manager.team,
        season=season,
        gameweek=9,
        current_rank=5,
        previous_rank=7,
        total_points=485,
        mini_league=league,
    )
    rival = Team.objects.create(fpl_team_id="202", team_name="The Visitors")
    TrackedTeam.objects.create(user=manager, team=rival)
    GameweekData.objects.create(team=rival, season=season, gameweek=10, points=50)
    SyncState.objects.create(
        key="context",
        kind="context",
        payload={"events": api.fetch_bootstrap_static.return_value["events"]},
    )
    GameweekData.objects.create(
        team=manager.team, season=season, gameweek=9, overall_rank=220000
    )
    GameweekData.objects.create(
        team=manager.team,
        season=season,
        gameweek=10,
        overall_rank=200000,
        points=53,
        event_transfers_cost=4,
    )
    client.post(reverse("rivals:rank-goal"), {"rank": "250000", "season": season})
    assert SeasonRankGoal.objects.get().rank == 250000
    page = client.get(reverse("rivals:dashboard"))
    assert page.context["brief"]["goal_gap"] == -50000
    assert page.context["brief"]["mine"]["movement"] == 20000
    assert page.context["brief"]["mine"]["net"] == 49
    assert "50,000 places inside target" in page.content.decode()
    assert "GW10 pending" in page.content.decode()
    assert page.context["brief"]["rivals"][0]["gap"]["swing"] == -1
    previous = client.get(reverse("rivals:dashboard"), {"week": 9})
    assert previous.context["brief"]["final"] and previous.context["brief"]["week"] == 9
    assert previous.context["brief"]["leagues"][0][1].rank_change == 2


def test_shared_captain_hits_and_bench_review():
    positions = [1, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 1, 2, 2, 3]
    rows = [
        SimpleNamespace(
            player_fpl_id=i,
            position=i + 1,
            points_final=True,
            player_name=f"P{i}",
            gameweek_points=1,
            gameweek_minutes=90,
            multiplier=int(i < 11),
            element_type=k,
            updated_at=NOW,
        )
        for i, k in enumerate(positions)
    ]
    rows[0].multiplier = 3
    rows[11].gameweek_points = 5
    mine = player_review(rows)
    assert mine["swap"] == ("P11", "P0", 12, 3)
    mine.update(net=9, history=SimpleNamespace(event_transfers_cost=4))
    rows[0].multiplier = 2
    rival = player_review(rows)
    rival.update(net=12, history=SimpleNamespace(event_transfers_cost=0))
    assert explain_gap(mine, rival) == {
        "swing": -3,
        "diffs": [("P0", 1, 3, 2)],
        "hits": -4,
    }
    for r in rows:
        r.multiplier = max(1, r.multiplier)
    assert player_review(rows)["bench_note"] == "All bench points counted."


def test_player_evidence_matches_published_history(client, manager, api):
    login(client, manager)
    for player in api.fetch_bootstrap_static.return_value["elements"]:
        player.update(starts=10, expected_goal_involvements="2.3", total_points=31)
    api.fetch_fixtures_data.return_value.append(
        {
            "id": 800,
            "event": 9,
            "finished": True,
            "team_h": 3,
            "team_a": 4,
            "team_h_score": 3,
            "team_a_score": 1,
        }
    )

    FplSnapshot.objects.create(
        key="element:2026/27:8",
        updated_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        payload={
            "history": [
                {
                    "fixture": 800,
                    "opponent_team": 4,
                    "minutes": 77,
                    "total_points": 8,
                    "starts": 1,
                }
            ]
        },
    )
    engine = DecisionEngine(manager, api).load()
    engine.prepare(engine.inputs())
    outgoing, incoming = comparison(engine, 8, 18)
    match = outgoing["fixtures"][0]["matches"][0]
    assert (
        match["season"],
        match["recent"],
        match["appearances"],
        match["minutes"],
        match["returns"],
    ) == (3, 3, 1, 77, 8)
    assert incoming["history"] == []
    with patch("rivals.services.decision_engine.FplApiService", return_value=api):
        page = client.get(reverse("rivals:decisions.players", args=[8, 18]))
        assert "77 min, 8 FPL pts" in page.content.decode()
        assert page.content.decode().count("<polygon") == 2
        solo = client.get(reverse("rivals:decisions.player", args=[8]))
        assert (
            solo.status_code == 200 and 'id="fixture-8-11-1"' in solo.content.decode()
        )
        assert len(solo.context["profiles"]) == 1


def test_saved_route_keeps_captain_and_bench_and_expires_on_squad_change(
    client, manager, api
):
    login(client, manager)
    engine = DecisionEngine(manager, api).load()
    lineup = {"xi": [1, 4, 5, 6, 8, 9, 10, 11, 13, 14, 15], "captain": 8, "vice": 1}
    planning = TeamPlanningInput.objects.create(
        user=manager,
        team=manager.team,
        season="2026/27",
        gameweek=11,
        squad_ids=engine.public_ids,
        planned_lineup=lineup,
    )
    payload = dict(
        form_data(engine), free_transfers=1, out_1=8, in_1=18, out_2=3, in_2=17
    )
    with patch("rivals.services.decision_engine.FplApiService", return_value=api):
        preview = client.post(reverse("rivals:decisions.workspace"), payload)
        option = preview.context["outlook_route"]
        week = preview.context["route_weeks"][0]
        assert option["hits"] == 4 and option["lineups"][0]["captain"] == 18
        assert 17 in {p["id"] for p in week["bench"]}
        assert option["immediate"]["points"] == round(
            option["lineups"][0]["points"] - 4, 1
        )
        client.post(reverse("rivals:decisions.save"), {"choice": option["save_token"]})
        TeamPlanningInput.objects.filter(pk=planning.pk).update(
            squad_ids=[18 if pid == 8 else pid for pid in engine.public_ids]
        )
        reload = client.get(reverse("rivals:decisions.summary"))
        assert "no longer fits" in reload.content.decode()
        assert DecisionRecord.objects.get().snapshot["chosen"]["key"] == option["key"]


def test_route_pitch_has_substitution_controls(client, manager, api):
    login(client, manager)
    engine = DecisionEngine(manager, api).load()
    with patch("rivals.services.decision_engine.FplApiService", return_value=api):
        response = client.post(
            reverse("rivals:decisions.workspace"),
            dict(form_data(engine), out_1=8, in_1=18),
        )
        route = (
            response.content.decode()
            .split('<div class="route-pitch">')[1]
            .split('<div class="hold-pitch">')[0]
        )
        assert route.count("data-lineup-select") == 15
        assert 'data-lineup-player="18" data-lineup-slot="8"' in route
        assert 'form="route-planned-lineup-form"' in route
        line = response.context["outlook_route"]["lineups"][0]
        starters = list(line["xi"])
        keeper = next(pid for pid in starters if pid in (1, 2))
        other_keeper = 3 - keeper
        starters[starters.index(keeper)] = other_keeper
        if 18 not in starters:
            midfielder = next(
                pid for pid in starters if engine.players[pid]["element_type"] == 3
            )
            starters[starters.index(midfielder)] = 18
        payload = {
            "gameweek": 11,
            "season": "2026/27",
            "mode": "planned",
            "preview_route": "8:18",
            "starters": [8 if pid == 18 else pid for pid in starters],
            "captain": 8,
            "vice": other_keeper,
        }
        edited = client.post(reverse("rivals:decisions.lineup"), payload)
        chosen = edited.context["outlook_route"]
        assert set(chosen["lineups"][0]["xi"]) == set(starters)
        assert chosen["lineups"][0]["captain"] == 18
        assert chosen["lineups"][0]["vice"] == other_keeper
        assert not TeamPlanningInput.objects.get().active_decision_id
        client.post(reverse("rivals:decisions.save"), {"choice": chosen["save_token"]})
        reopened = client.get(reverse("rivals:decisions.preview"))
        assert reopened.context["outlook_route"]["lineups"][0] == chosen["lineups"][0]
