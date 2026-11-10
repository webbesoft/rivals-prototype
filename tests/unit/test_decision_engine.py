from copy import deepcopy
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from time import perf_counter
from unittest.mock import Mock, patch

import pytest
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from rivals.forms import DecisionInputForm
from rivals.models import (
    DecisionRecord,
    GameweekData,
    MiniLeague,
    MiniLeagueTeam,
    PlannedTransfer,
    Player,
    SquadHistory,
    Team,
    TeamPlanningInput,
    Transfer,
    TransferPlan,
    UserMiniLeague,
)
from rivals.services.decision_engine import DecisionEngine, digest
from rivals.services.decision_review_service import calibration, review_decisions
from rivals.services.fpl_api_service import FplApiService

NOW = datetime(2026, 11, 10, 12, tzinfo=dt_timezone.utc)


@pytest.fixture(autouse=True)
def isolated():
    cache.clear()
    with (
        patch("django.utils.timezone.now", return_value=NOW),
        patch("requests.get", side_effect=AssertionError("Unexpected live HTTP")),
    ):
        yield
    cache.clear()


@pytest.fixture
def manager(db):
    team = Team.objects.create(
        fpl_team_id="101", team_name="My XI", current_season="2026/27", bank=10
    )
    user = get_user_model().objects.create_user(
        username="manager",
        email="manager@example.com",
        password="test123456",
        team=team,
    )
    types = [1, 1] + [2] * 5 + [3] * 5 + [4] * 3
    SquadHistory.objects.bulk_create(
        [
            SquadHistory(
                team=team,
                season="2026/27",
                gameweek=10,
                player_fpl_id=pid,
                position=pid,
                element_type=pos,
                multiplier=1 if pid <= 11 else 0,
                player_name=f"Player {pid}",
                player_cost=50,
            )
            for pid, pos in enumerate(types, 1)
        ]
    )
    return user


@pytest.fixture
def api():
    api = Mock(spec=FplApiService)
    events = [
        {"id": 1, "deadline_time": "2026-08-15T12:00:00Z", "finished": True},
        {"id": 10, "deadline_time": "2026-11-07T12:00:00Z", "finished": False},
    ]
    for week in range(11, 17):
        events.append(
            {
                "id": week,
                "deadline_time": (
                    NOW + timedelta(days=4 + (week - 11) * 7)
                ).isoformat(),
                "finished": False,
            }
        )
    types = [1, 1] + [2] * 5 + [3] * 5 + [4] * 3
    players = [
        {
            "id": pid,
            "web_name": f"Player {pid}",
            "element_type": types[pid - 1] if pid <= 15 else 1 + (pid - 16) % 4,
            "team": 1 + (pid - 1) // 3,
            "now_cost": 50 if pid <= 15 else 55,
            "minutes": 900,
            "form": "3.0" if pid <= 15 else "8.0",
            "points_per_game": "3.0" if pid <= 15 else "8.0",
            "status": "a",
            "chance_of_playing_next_round": 100,
            "selected_by_percent": "1.0",
        }
        for pid in range(1, 46)
    ]
    fixtures = [
        {
            "event": week,
            "team_h": home,
            "team_a": home + 1,
            "team_h_difficulty": 3,
            "team_a_difficulty": 3,
            "finished": False,
        }
        for week in range(11, 17)
        for home in range(1, 20, 2)
    ]
    api.fetch_bootstrap_static.return_value = {
        "events": events,
        "elements": players,
        "teams": [{"id": club, "short_name": f"Club {club}"} for club in range(1, 21)],
    }
    api.fetch_fixtures_data.return_value = fixtures
    return api


def confirmed(engine, **changes):
    data = engine.inputs()
    data.update(
        confirmed=True,
        bank=10,
        free_transfers=2,
        horizon=3,
        sales={str(pid): 50 for pid in engine.public_ids},
    )
    data.update(changes)
    return data


def form_data(engine):
    data = {
        "season": "2026/27",
        "gameweek": 11,
        "bank": "1.0",
        "free_transfers": 2,
        "horizon": 3,
        "strategy": "protect",
        "squad_confirmed": "on",
    }
    for index, pid in enumerate(engine.public_ids):
        data[f"owned_{index}"] = pid
        data[f"sell_{index}"] = "5.0"
    return data


@pytest.mark.django_db
def test_horizon_counts_blanks_and_doubles_instead_of_next_five_matches(manager, api):
    engine = DecisionEngine(manager, api).load()
    api.fetch_fixtures_data.return_value[:] = [
        fixture
        for fixture in api.fetch_fixtures_data.return_value
        if not (fixture["event"] == 11 and fixture["team_h"] == 1)
    ]
    engine.load().prepare(confirmed(engine))
    assert engine.projections[1][11] == 0
    assert engine.projections[1][12] > 0
    double = {
        "event": 12,
        "team_h": 1,
        "team_a": 3,
        "team_h_difficulty": 3,
        "finished": False,
    }
    api.fetch_fixtures_data.return_value.append(double)
    engine.load().prepare(confirmed(engine))
    assert engine.projections[1][12] == pytest.approx(2 * engine.projections[1][13])


@pytest.mark.django_db
def test_best_xi_is_legal_and_bench_is_not_added_to_forecast(manager, api):
    engine = DecisionEngine(manager, api).load()
    engine.prepare(confirmed(engine))
    result = engine.evaluate(engine.ids)
    for line in result["lineups"]:
        xi = line["xi"]
        assert len(xi) == len(set(xi)) == 11
        assert sum(engine.players[pid]["element_type"] == 1 for pid in xi) == 1
        assert sum(engine.players[pid]["element_type"] == 2 for pid in xi) >= 3
        expected = (
            sum(engine.projections[pid][line["gameweek"]] for pid in xi)
            + engine.projections[line["captain"]][line["gameweek"]]
        )
        assert line["points"] == pytest.approx(expected, abs=0.01)


@pytest.mark.django_db
def test_hit_cost_and_zero_free_transfers_are_applied_once(manager, api):
    engine = DecisionEngine(manager, api).load()
    engine.prepare(confirmed(engine, free_transfers=0))
    baseline = engine.evaluate(engine.ids)
    plan = engine.candidate([(8, 18), (3, 17)], baseline)
    assert plan["hits"] == 8
    assert plan["net_gain"] == pytest.approx(plan["gross_gain"] - 8, abs=0.11)
    assert plan["bank_after"] == 0
    assert plan["confirmed"]
    assert engine.candidate([], baseline)["hits"] == 0


@pytest.mark.django_db
def test_defaults_produce_usable_options_without_private_input(manager, api):
    report = DecisionEngine(manager, api).report()
    assert report["inputs"]["free_transfers"] == 1
    assert report["inputs"]["bank"] == 10
    assert len(report["inputs"]["sales"]) == 15
    assert report["best"]["moves"]
    assert all(option["hits"] is not None for option in report["options"])
    assert report["baseline"]["net_gain"] == 0


@pytest.mark.django_db
def test_actual_selling_price_is_used_instead_of_market_price(manager, api):
    engine = DecisionEngine(manager, api).load()
    data = confirmed(engine, bank=0)
    data["sales"]["8"] = 40
    engine.prepare(data)
    assert engine.candidate([(8, 18)], engine.evaluate(engine.ids)) is None
    data["bank"] = 15
    engine.prepare(data)
    assert engine.candidate([(8, 18)], engine.evaluate(engine.ids))["bank_after"] == 0


@pytest.mark.django_db
def test_funding_pair_can_be_affordable_when_single_upgrade_is_not(manager, api):
    engine = DecisionEngine(manager, api).load()
    engine.players[17]["now_cost"] = 40
    engine.players[18]["now_cost"] = 60
    engine.prepare(confirmed(engine, bank=0))
    baseline = engine.evaluate(engine.ids)
    assert engine.candidate([(8, 18)], baseline) is None
    route = engine.candidate([(3, 17), (8, 18)], baseline)
    assert route and route["bank_after"] == 0


@pytest.mark.django_db
def test_simultaneous_transfers_validate_final_club_counts(manager, api):
    engine = DecisionEngine(manager, api).load()
    engine.players[18]["team"] = 1
    engine.prepare(confirmed(engine))
    base = engine.evaluate(engine.ids)
    assert engine.candidate([(8, 18)], base) is None
    assert engine.candidate([(3, 17), (8, 18)], base)
    assert engine.candidate([(8, 18), (9, 18)], base) is None
    assert engine.candidate([(8, 17)], base) is None
    assert engine.candidate([(8, 9)], base) is None


@pytest.mark.django_db
def test_unavailable_and_zero_minutes_players_are_not_upgrades(manager, api):
    engine = DecisionEngine(manager, api).load()
    engine.players[18]["status"] = "i"
    engine.players[22]["minutes"] = 0
    report = engine.report(confirmed(engine))
    assert engine.projections[18][11] == engine.projections[22][11] == 0
    assert all(
        move["in"] not in (18, 22)
        for choice in report["options"]
        for move in choice["moves"]
    )


@pytest.mark.django_db
def test_roll_preserves_free_transfer_flexibility_and_cap(manager, api):
    engine = DecisionEngine(manager, api).load()
    engine.prepare(confirmed(engine, free_transfers=5))
    base = engine.evaluate(engine.ids)
    assert engine.candidate([], base)["free_next"] == 5
    assert engine.candidate([(8, 18), (3, 17)], base)["free_next"] == 4


@pytest.mark.django_db
def test_estimated_squad_does_not_block_a_positive_transfer_route(manager, api):
    engine = DecisionEngine(manager, api).load()
    report = engine.report(confirmed(engine, confirmed=False))
    assert report["best"]["moves"]
    assert report["best"]["net_gain"] > 0


@pytest.mark.django_db
def test_manual_route_is_compared_with_hold_and_maximum_two_moves(manager, api):
    engine = DecisionEngine(manager, api).load()
    report = engine.report(confirmed(engine, manual=[[8, 18], [3, 17]]))
    assert report["manual"]["key"] == "3:17,8:18"
    assert any(option["key"] == report["manual"]["key"] for option in report["options"])
    with pytest.raises(ValueError, match="at most two"):
        engine.report(confirmed(engine, manual=[[8, 18], [3, 17], [13, 19]]))


@pytest.mark.django_db
def test_protect_and_chase_change_risk_weight_without_rewarding_low_ownership(
    manager, api
):
    engine = DecisionEngine(manager, api).load()
    engine.prepare(confirmed(engine, strategy="protect"))
    base = engine.evaluate(engine.ids)
    protect = engine.candidate([(8, 18)], base)
    engine.inputs_used["strategy"] = "chase"
    chase = engine.candidate([(8, 18)], base)
    assert chase["objective"] > protect["objective"]
    before = chase["net_gain"]
    engine.players[18]["selected_by_percent"] = "99.0"
    assert engine.candidate([(8, 18)], base)["net_gain"] == before


@pytest.mark.django_db
def test_rival_scenarios_require_subscription_and_use_current_season_squad(
    manager, api
):
    league = MiniLeague.objects.create(
        fpl_league_id="88", name="Target league", last_synced_at=NOW
    )
    UserMiniLeague.objects.create(user=manager, mini_league=league)
    rival = Team.objects.create(fpl_team_id="102", team_name="Rival")
    MiniLeagueTeam.objects.create(
        team=manager.team,
        mini_league=league,
        season="2026/27",
        current_rank=2,
        total_points=500,
    )
    MiniLeagueTeam.objects.create(
        team=rival,
        mini_league=league,
        season="2026/27",
        current_rank=1,
        total_points=520,
    )
    rows = list(manager.team.squad_histories.all())
    for row in rows:
        row.pk = None
        row.team = rival
        if row.player_fpl_id == 8:
            row.player_fpl_id = 18
    SquadHistory.objects.bulk_create(rows)
    engine = DecisionEngine(manager, api).load()
    report = engine.report(confirmed(engine, league_id=league.pk, rival_id=rival.pk))
    assert report["rival"]["gap"] == 20 and report["rival"]["shared"] == 14
    assert report["rival"]["rival"] == "Rival"
    other_league = MiniLeague.objects.create(fpl_league_id="89", name="Private league")
    assert engine.report(confirmed(engine, league_id=other_league.pk))["rival"] is None


@pytest.mark.django_db
def test_editable_financial_estimates_carry_forward_but_stay_private(manager, api):
    TeamPlanningInput.objects.create(
        user=manager,
        team=manager.team,
        season="2026/27",
        gameweek=10,
        free_transfers=5,
        bank="20.0",
        squad_confirmed=True,
        squad_ids=list(range(1, 16)),
        selling_prices={"8": 50},
        strategy="chase",
        horizon=6,
    )
    engine = DecisionEngine(manager, api).load()
    data = engine.inputs()
    assert (
        data["free_transfers"] == 5
        and data["bank"] == 200
        and len(data["sales"]) == 15
        and data["confirmed"]
    )
    assert data["horizon"] == 6 and data["strategy"] == "chase"
    other = get_user_model().objects.create_user(
        username="other",
        email="other@example.com",
        password="test123456",
        team=manager.team,
    )
    assert DecisionEngine(other, api).load().inputs()["strategy"] == "protect"


@pytest.mark.django_db
def test_form_rejects_wrong_squad_prices_deadline_and_foreign_league(manager, api):
    engine = DecisionEngine(manager, api).load()
    data = form_data(engine)
    assert DecisionInputForm(data, engine=engine).is_valid()
    invalid = {**data, "owned_1": data["owned_0"], "sell_0": "9.0", "gameweek": 10}
    assert not DecisionInputForm(invalid, engine=engine).is_valid()
    league = MiniLeague.objects.create(fpl_league_id="99", name="Other")
    assert not DecisionInputForm(
        {**data, "target_league": league.pk}, engine=engine
    ).is_valid()


@pytest.mark.django_db
def test_workspace_renders_and_saves_confirmed_inputs_without_executing_transfers(
    manager, api
):
    client = Client()
    client.force_login(manager)
    with patch("rivals.services.decision_engine.FplApiService", return_value=api):
        get = client.get(reverse("rivals:decisions.workspace"))
        assert get.status_code == 200 and "Compare options" in get.content.decode()
        engine = DecisionEngine(manager, api).load()
        response = client.post(reverse("rivals:decisions.workspace"), form_data(engine))
    assert response.status_code == 200
    assert "Estimated bank after moves" in response.content.decode()
    saved = TeamPlanningInput.objects.get(user=manager)
    assert saved.squad_confirmed and saved.bank is None and saved.free_transfers == 2
    assert saved.free_transfers_recorded_at == NOW
    assert saved.selling_prices == {}  # Unchanged estimates are not manual overrides.
    assert not DecisionRecord.objects.exists()


@pytest.mark.django_db
def test_journal_signed_choices_are_private_idempotent_and_reviewable(manager, api):
    client = Client()
    client.force_login(manager)
    engine = DecisionEngine(manager, api).load()
    inputs = confirmed(engine)
    TeamPlanningInput.objects.create(
        user=manager,
        team=manager.team,
        season="2026/27",
        gameweek=11,
        free_transfers=2,
        bank="1.0",
        squad_confirmed=True,
        squad_ids=inputs["ids"],
        selling_prices=inputs["sales"],
        horizon=3,
    )
    engine.load()
    report = engine.report()
    option = next(option for option in report["options"] if option["moves"])
    token = signing.dumps(
        {
            "user": manager.pk,
            "team": manager.team_id,
            "fingerprint": report["fingerprint"],
            "option": option["key"],
        },
        salt="decision",
    )
    with patch("rivals.services.decision_engine.FplApiService", return_value=api):
        assert (
            client.post(reverse("rivals:decisions.save"), {"choice": token}).status_code
            == 302
        )
        client.post(reverse("rivals:decisions.save"), {"choice": token})
    assert DecisionRecord.objects.count() == 1
    record = DecisionRecord.objects.get()
    assert record.snapshot["chosen"]["key"] == option["key"]
    assert client.get(reverse("rivals:decisions.journal")).status_code == 200
    other = get_user_model().objects.create_user(
        username="other",
        email="other@example.com",
        password="test123456",
        team=manager.team,
    )
    client.force_login(other)
    assert (
        client.post(
            reverse("rivals:decisions.status", args=[record.pk]), {"status": "acted"}
        ).status_code
        == 404
    )
    client.post(reverse("rivals:decisions.save"), {"choice": token})
    assert DecisionRecord.objects.count() == 1
    assert not client.get(reverse("rivals:decisions.journal")).context["decisions"]


@pytest.mark.django_db
def test_review_uses_frozen_lineups_and_only_final_scores(manager, api):
    engine = DecisionEngine(manager, api).load()
    report = engine.report(confirmed(engine, manual=[[8, 18]]))
    chosen = report["manual"]
    record = DecisionRecord.objects.create(
        user=manager,
        team=manager.team,
        season="2026/27",
        gameweek=11,
        fingerprint="frozen",
        snapshot={"report": report, "chosen": chosen},
    )
    for event in api.fetch_bootstrap_static.return_value["events"]:
        if event["id"] in report["weeks"]:
            event.update(finished=True, data_checked=True)
    api.fetch_gameweek_live.return_value = {
        "elements": [
            {"id": pid, "stats": {"total_points": pid % 7}} for pid in engine.players
        ]
    }
    assert review_decisions(api) == 1
    record.refresh_from_db()
    assert record.outcomes["complete"] and record.outcomes["completed"] == 3
    expected = 0
    for base, selected in zip(report["baseline"]["lineups"], chosen["lineups"]):
        expected += sum(pid % 7 for pid in selected["xi"]) + selected["captain"] % 7
        expected -= sum(pid % 7 for pid in base["xi"]) + base["captain"] % 7
    assert record.outcomes["net_gain"] == expected - chosen["hits"]
    assert calibration([record])["completed_plans"] == 1
    api.fetch_gameweek_live.reset_mock()
    review_decisions(api)
    api.fetch_gameweek_live.assert_not_called()


@pytest.mark.django_db
def test_review_does_not_query_another_season_or_make_up_missing_scores(manager, api):
    engine = DecisionEngine(manager, api).load()
    report = engine.report(confirmed(engine))
    old = DecisionRecord.objects.create(
        user=manager,
        team=manager.team,
        season="2025/26",
        gameweek=11,
        fingerprint="old",
        snapshot={"report": report, "chosen": report["baseline"]},
    )
    review_decisions(api)
    api.fetch_gameweek_live.assert_not_called()
    old.refresh_from_db()
    assert old.outcomes == {}


@pytest.mark.django_db
def test_cache_is_input_sensitive_and_reports_correct_fixture_window(manager, api):
    engine = DecisionEngine(manager, api).load()
    first = engine.report(confirmed(engine))
    changed = engine.report(confirmed(engine, free_transfers=0))
    assert first["fingerprint"] != changed["fingerprint"]
    assert first["weeks"] == [11, 12, 13]
    assert first["baseline"]["net_gain"] == 0


@pytest.mark.django_db
def test_dashboard_and_planner_shells_do_not_wait_for_fpl(manager):
    client = Client()
    client.force_login(manager)
    assert client.get(reverse("rivals:dashboard")).status_code == 200
    response = client.get(reverse("rivals:decisions"))
    assert (
        response.status_code == 200
        and "decision-workspace" in response.content.decode()
    )


@pytest.mark.django_db
def test_representative_forecast_runtime(manager, api):
    base = api.fetch_bootstrap_static.return_value["elements"][0]
    for pid in range(46, 801):
        api.fetch_bootstrap_static.return_value["elements"].append(
            {
                **base,
                "id": pid,
                "element_type": 1 + pid % 4,
                "team": 1 + pid % 20,
                "form": str(3 + pid % 5),
                "now_cost": 40 + pid % 50,
            }
        )
    engine = DecisionEngine(manager, api).load()
    start = perf_counter()
    report = engine.report(confirmed(engine, horizon=6))
    elapsed = perf_counter() - start
    assert report["options"] and len(report["weeks"]) == 6
    print(f"800 players / six weeks: {elapsed * 1000:.1f} ms with mocked upstream data")


@pytest.mark.django_db
def test_source_failure_uses_plain_messages_and_keeps_saved_inputs(manager, api):
    TeamPlanningInput.objects.create(
        user=manager, team=manager.team, season="2026/27", gameweek=11, free_transfers=3
    )
    api.fetch_bootstrap_static.return_value = None
    client = Client()
    client.force_login(manager)
    with patch("rivals.services.decision_engine.FplApiService", return_value=api):
        response = client.get(reverse("rivals:decisions.summary"))
    assert "try again" in response.content.decode()
    assert TeamPlanningInput.objects.get(user=manager).free_transfers == 3


@pytest.mark.django_db
def test_forms_require_csrf_and_journal_token_tampering_is_rejected(manager):
    client = Client(enforce_csrf_checks=True)
    client.force_login(manager)
    assert client.post(reverse("rivals:decisions.workspace"), {}).status_code == 403
    assert (
        client.post(reverse("rivals:decisions.save"), {"choice": "forged"}).status_code
        == 403
    )
    client = Client()
    client.force_login(manager)
    client.post(reverse("rivals:decisions.save"), {"choice": "forged"})
    assert not DecisionRecord.objects.exists()


@pytest.mark.django_db
def test_summary_cannot_reuse_a_revoked_league_from_cache(manager, api):
    league = MiniLeague.objects.create(fpl_league_id="88", name="Target")
    member = UserMiniLeague.objects.create(user=manager, mini_league=league)
    engine = DecisionEngine(manager, api).load()
    first = engine.report(confirmed(engine, league_id=league.pk))
    member.active = False
    member.save(update_fields=["active"])
    next_report = engine.report(confirmed(engine, league_id=league.pk))
    assert first["fingerprint"] != next_report["fingerprint"]
    assert next_report["rival"] is None


@pytest.mark.django_db
def test_backtest_waits_for_checked_scores_and_excludes_zero_error_holds(manager, api):
    engine = DecisionEngine(manager, api).load()
    report = engine.report(confirmed(engine))
    hold = DecisionRecord.objects.create(
        user=manager,
        team=manager.team,
        season="2026/27",
        gameweek=11,
        fingerprint="hold",
        snapshot={"report": report, "chosen": report["baseline"]},
    )
    for event in api.fetch_bootstrap_static.return_value["events"]:
        event["finished"] = True
        event["data_checked"] = False
    review_decisions(api)
    api.fetch_gameweek_live.assert_not_called()
    hold.outcomes = {"complete": True, "forecast_error": 0}
    assert calibration([hold])["completed_plans"] == 0


@pytest.mark.django_db
def test_partial_results_and_unknown_hits_never_invent_net_outcomes(manager, api):
    engine = DecisionEngine(manager, api).load()
    report = engine.report(confirmed(engine, free_transfers=None))
    chosen = next(item for item in report["options"] if item["moves"])
    record = DecisionRecord.objects.create(
        user=manager,
        team=manager.team,
        season="2026/27",
        gameweek=11,
        fingerprint="partial",
        snapshot={"report": report, "chosen": chosen},
    )
    event = next(
        event
        for event in api.fetch_bootstrap_static.return_value["events"]
        if event["id"] == 11
    )
    event.update(finished=True, data_checked=True)
    api.fetch_gameweek_live.return_value = {
        "elements": [
            {"id": pid, "stats": {"total_points": 2}} for pid in engine.players
        ]
    }
    review_decisions(api)
    record.refresh_from_db()
    assert record.outcomes["completed"] == 1 and not record.outcomes["complete"]
    assert (
        record.outcomes["net_gain"] is None
        and record.outcomes["forecast_error"] is None
    )


@pytest.mark.django_db
def test_plan_api_applies_owned_plan_and_checks_season_and_ownership(manager, api):
    outgoing = Player.objects.create(fpl_id=8, web_name="Out", element_type=3)
    incoming = Player.objects.create(fpl_id=18, web_name="In", element_type=3)
    plan = TransferPlan.objects.create(
        user=manager, team=manager.team, season="2026/27"
    )
    PlannedTransfer.objects.create(
        plan=plan, gameweek=11, player_out=outgoing, player_in=incoming
    )
    client = Client()
    client.force_login(manager)
    url = reverse("api:projections", args=[manager.team_id])
    with patch("rivals.services.decision_engine.FplApiService", return_value=api):
        response = client.get(url, {"plan_id": plan.pk})
        assert (
            response.status_code == 200
            and response.json()["manual"]["moves"][0]["in"] == 18
        )
        plan.season = "2025/26"
        plan.save()
        assert client.get(url, {"plan_id": plan.pk}).status_code == 400
    other = get_user_model().objects.create_user(
        username="other",
        email="other@example.com",
        password="test123456",
        team=manager.team,
    )
    client.force_login(other)
    assert client.get(url, {"plan_id": plan.pk}).status_code == 404


@pytest.mark.django_db
def test_api_cannot_create_a_plan_for_someone_elses_team(manager, api):
    other_team = Team.objects.create(fpl_team_id="999", team_name="Other")
    client = Client()
    client.force_login(manager)
    response = client.post(
        reverse("api:plans_list_create"),
        {"team": other_team.pk, "name": "Unauthorized"},
    )
    assert response.status_code == 400
    assert not TransferPlan.objects.exists()


@pytest.mark.django_db
def test_changing_current_holdings_clears_carried_over_confirmations(manager, api):
    engine = DecisionEngine(manager, api).load()
    data = form_data(engine)
    data["owned_7"] = 18
    data["sell_7"] = "5.5"
    form = DecisionInputForm(data, engine=engine)
    assert form.is_valid()
    assert form.cleaned_data["ids"][7] == 18
    assert form.cleaned_data["sales"]["18"] == 55
    assert "8" not in form.cleaned_data["sales"]
    assert "sell_7.value = ''" in form.fields["owned_7"].widget.attrs["onchange"]


@pytest.mark.django_db
def test_protecting_against_a_rival_rewards_reducing_shared_exposure(manager, api):
    engine = DecisionEngine(manager, api).load()
    engine.prepare(confirmed(engine, strategy="protect"))
    baseline = engine.evaluate(engine.ids)
    rival_ids = [pid if pid != 8 else 18 for pid in engine.ids]
    engine._rival_weights = engine.lineup_weights(engine.evaluate(rival_ids)["lineups"])
    match = engine.candidate([(8, 18)], baseline)
    assert match["relative_risk_change"] < 0
    assert match["objective"] > match["net_gain"]


@pytest.mark.django_db
def test_main_recommendations_cover_positions_and_never_repeat_the_same_outgoing_player(
    manager, api
):
    engine = DecisionEngine(manager, api).load()
    report = engine.report(confirmed(engine))
    outgoing = [move["out"] for option in report["options"] for move in option["moves"]]
    assert len(outgoing) == len(set(outgoing))
    assert {engine.players[pid]["element_type"] for pid in outgoing} == {1, 2, 3, 4}
    assert report["best"]["key"] in {option["key"] for option in report["options"]}
    slot_positions = {slot["position"] for slot in report["alternatives"]}
    assert slot_positions == {"GKP", "DEF", "MID", "FWD"}


@pytest.mark.django_db
def test_many_alternatives_for_one_poor_defender_are_grouped_as_one_recommendation(
    manager, api
):
    engine = DecisionEngine(manager, api).load()
    engine.players[3].update(status="i", form="0", points_per_game="0", minutes=0)

    def shortlist(baseline):
        return [
            engine.candidate([move], baseline)
            for move in ((3, 17), (3, 21), (3, 25), (8, 18))
        ]

    with patch.object(engine, "options", side_effect=shortlist):
        report = engine.report(confirmed(engine))
    defender_variants = [
        option
        for option in report["options"]
        if any(move["out"] == 3 for move in option["moves"])
    ]
    assert len(defender_variants) == 1
    grouped = next(
        slot for slot in report["alternatives"] if slot["player"] == engine.name(3)
    )
    assert len(grouped["options"]) == 3
    assert any(
        any(move["out"] == 8 for move in option["moves"])
        for option in report["options"]
    )


@pytest.mark.django_db
def test_diversity_does_not_force_a_points_losing_upgrade_of_an_injured_bench_player(
    manager, api
):
    engine = DecisionEngine(manager, api).load()
    for pid in range(1, 16):
        engine.players[pid].update(form="9.0", points_per_game="9.0")
    engine.players[3].update(status="i", form="0", points_per_game="0", minutes=0)
    report = engine.report(confirmed(engine))
    assert report["best"]["key"] == "hold"
    assert all(
        option["net_gain"] > 0 for option in report["options"] if option["moves"]
    )


@pytest.mark.django_db
def test_price_drop_invalidates_a_previously_confirmed_selling_price(manager, api):
    TeamPlanningInput.objects.create(
        user=manager,
        team=manager.team,
        season="2026/27",
        gameweek=11,
        bank="1.0",
        free_transfers=2,
        squad_confirmed=True,
        squad_ids=list(range(1, 16)),
        selling_prices={"8": 50},
    )
    api.fetch_bootstrap_static.return_value["elements"][7]["now_cost"] = 49
    engine = DecisionEngine(manager, api).load()
    assert engine.inputs()["sales"]["8"] == 49
    engine.prepare(engine.inputs())
    route = engine.candidate([(8, 18)], engine.evaluate(engine.ids))
    assert route and route["bank_after"] >= 0
    assert not route["unknown"]


def defaults_for(manager, api, target=11):
    from rivals.services.planning_defaults_service import PlanningDefaults

    from rivals.services.gameweek_context_service import GameweekContextService

    bootstrap = api.fetch_bootstrap_static()
    context = {
        **GameweekContextService.derive(bootstrap["events"]),
        "decision_gameweek": target,
        "events": bootstrap["events"],
    }
    return PlanningDefaults(
        manager, context, {p["id"]: p for p in bootstrap["elements"]}
    )


def initial_payload(form):
    return {
        key: value if value is not None else "" for key, value in form.initial.items()
    }


@pytest.mark.django_db
@pytest.mark.parametrize(
    "count,used,expected", [(2, 0, 3), (2, 1, 2), (2, 2, 1), (2, 4, 1), (5, 0, 5)]
)
def test_seeded_free_transfers_roll_forward_and_account_for_each_move(
    manager, api, count, used, expected
):
    timestamp = NOW - timedelta(days=5)
    TeamPlanningInput.objects.create(
        user=manager,
        team=manager.team,
        season="2026/27",
        gameweek=10,
        free_transfers=count,
        free_transfers_recorded_at=timestamp,
    )
    for index in range(used):
        Transfer.objects.create(
            team=manager.team,
            season="2026/27",
            gameweek=10,
            transfer_time=timestamp + timedelta(hours=1),
            player_in_name=f"In {index}",
            player_out_name=f"Out {index}",
        )
    assert defaults_for(manager, api).free_transfers() == expected


@pytest.mark.django_db
def test_midweek_seed_does_not_deduct_moves_already_included_in_the_count(manager, api):
    timestamp = NOW - timedelta(days=5)
    TeamPlanningInput.objects.create(
        user=manager,
        team=manager.team,
        season="2026/27",
        gameweek=10,
        free_transfers=2,
        free_transfers_recorded_at=timestamp,
    )
    for when in (timestamp - timedelta(hours=1), timestamp + timedelta(hours=1)):
        Transfer.objects.create(
            team=manager.team,
            season="2026/27",
            gameweek=10,
            transfer_time=when,
            player_in_name="In",
            player_out_name="Out",
        )
    GameweekData.objects.create(
        team=manager.team, season="2026/27", gameweek=10, event_transfers=2
    )
    assert defaults_for(manager, api).free_transfers() == 2


@pytest.mark.django_db
@pytest.mark.parametrize(
    "chip,expected", [("wildcard", 3), ("freehit", 3), ("bboost", 2), ("3xc", 2)]
)
def test_chip_weeks_preserve_transfer_stock_or_use_normal_rules(
    manager, api, chip, expected
):
    TeamPlanningInput.objects.create(
        user=manager,
        team=manager.team,
        season="2026/27",
        gameweek=10,
        free_transfers=3,
        free_transfers_recorded_at=NOW - timedelta(days=5),
    )
    GameweekData.objects.create(
        team=manager.team,
        season="2026/27",
        gameweek=10,
        chip_name=chip,
        event_transfers=2,
    )
    for index in range(2):
        Transfer.objects.create(
            team=manager.team,
            season="2026/27",
            gameweek=10,
            transfer_time=NOW - timedelta(days=4),
            player_in_name=f"In {index}",
            player_out_name="Out",
        )
    assert defaults_for(manager, api).free_transfers() == expected


@pytest.mark.django_db
def test_unseeded_allowance_uses_recorded_gameweek_counts_and_not_initial_unlimited_moves(
    manager, api
):
    manager.team.started_event = 9
    manager.team.save()
    GameweekData.objects.create(
        team=manager.team, season="2026/27", gameweek=9, event_transfers=15
    )
    GameweekData.objects.create(
        team=manager.team, season="2026/27", gameweek=10, event_transfers=0
    )
    assert defaults_for(manager, api).free_transfers() == 2


@pytest.mark.django_db
def test_history_bank_is_default_and_newer_manual_correction_wins(manager, api):
    GameweekData.objects.create(
        team=manager.team, season="2026/27", gameweek=10, bank=25
    )
    assert defaults_for(manager, api).bank() == 25
    TeamPlanningInput.objects.create(
        user=manager,
        team=manager.team,
        season="2026/27",
        gameweek=11,
        bank="1.7",
        bank_recorded_at=NOW,
    )
    assert defaults_for(manager, api).bank() == 17


@pytest.mark.django_db
def test_observed_transfers_adjust_bank_without_recounting_earlier_trades(manager, api):
    timestamp = NOW - timedelta(hours=2)
    TeamPlanningInput.objects.create(
        user=manager,
        team=manager.team,
        season="2026/27",
        gameweek=11,
        bank="2.0",
        bank_recorded_at=timestamp,
    )
    for when in (timestamp - timedelta(hours=1), timestamp + timedelta(hours=1)):
        Transfer.objects.create(
            team=manager.team,
            season="2026/27",
            gameweek=11,
            transfer_time=when,
            player_in_cost=60,
            player_out_cost=50,
            player_in_name="In",
            player_out_name="Out",
        )
    assert defaults_for(manager, api).bank() == 10


@pytest.mark.django_db
@pytest.mark.parametrize("current,expected", [(55, 52), (56, 53), (48, 48)])
def test_selling_prices_use_acquisition_cost_and_half_of_profit(
    manager, api, current, expected
):
    player = Player.objects.create(fpl_id=8, web_name="Owned", element_type=3)
    Transfer.objects.create(
        team=manager.team,
        season="2026/27",
        gameweek=8,
        player_in=player,
        player_in_cost=50,
        transfer_time=NOW - timedelta(days=20),
        player_out_name="Out",
    )
    api.fetch_bootstrap_static.return_value["elements"][7]["now_cost"] = current
    assert defaults_for(manager, api).selling_price(8) == expected


@pytest.mark.django_db
def test_opening_price_change_estimates_sale_when_purchase_history_is_missing(
    manager, api
):
    api.fetch_bootstrap_static.return_value["elements"][7].update(
        now_cost=55, cost_change_start=5
    )
    assert defaults_for(manager, api).selling_price(8) == 52


@pytest.mark.django_db
def test_default_submission_does_not_create_financial_seeds_or_fifteen_price_overrides(
    manager, api
):
    client = Client()
    client.force_login(manager)
    with patch("rivals.services.decision_engine.FplApiService", return_value=api):
        response = client.get(reverse("rivals:decisions.workspace"))
        form = response.context["form"]
        payload = initial_payload(form)
        client.post(reverse("rivals:decisions.workspace"), payload)
        client.post(reverse("rivals:decisions.workspace"), payload)
    saved = TeamPlanningInput.objects.get(user=manager)
    assert saved.free_transfers is None and saved.free_transfers_recorded_at is None
    assert saved.bank is None and saved.bank_recorded_at is None
    assert saved.selling_prices == {}


@pytest.mark.django_db
def test_user_correction_becomes_anchor_once_and_preferences_do_not_move_it(
    manager, api
):
    client = Client()
    client.force_login(manager)
    with patch("rivals.services.decision_engine.FplApiService", return_value=api):
        form = client.get(reverse("rivals:decisions.workspace")).context["form"]
        payload = {**initial_payload(form), "free_transfers": 2, "bank": "2.3"}
        client.post(reverse("rivals:decisions.workspace"), payload)
        saved = TeamPlanningInput.objects.get(user=manager)
        assert saved.free_transfers == 2 and saved.bank == Decimal("2.3")
        original_time = saved.free_transfers_recorded_at
        with patch("django.utils.timezone.now", return_value=NOW + timedelta(hours=1)):
            form = client.get(reverse("rivals:decisions.workspace")).context["form"]
            client.post(
                reverse("rivals:decisions.workspace"),
                {**initial_payload(form), "strategy": "chase"},
            )
    saved.refresh_from_db()
    assert saved.free_transfers_recorded_at == original_time


@pytest.mark.django_db
def test_planner_opens_with_results_and_no_confirmation_tasks(manager, api):
    client = Client()
    client.force_login(manager)
    with patch("rivals.services.decision_engine.FplApiService", return_value=api):
        response = client.get(reverse("rivals:decisions.workspace"))
    html = response.content.decode()
    assert html.index("Before the next deadline") < html.index("Adjust estimates")
    assert "Needs confirmation" not in html and "Confirm your squad" not in html
    assert 'type="checkbox"' not in html
    assert len(response.context["report"]["inputs"]["sales"]) == 15


@pytest.mark.django_db
def test_free_hit_squad_reverts_to_permanent_players_for_next_plan(manager, api):
    previous = list(manager.team.squad_histories.all())
    for row in previous:
        row.pk = None
        row.gameweek = 9
    SquadHistory.objects.bulk_create(previous)
    manager.team.squad_histories.filter(gameweek=10, player_fpl_id=8).update(
        player_fpl_id=18
    )
    GameweekData.objects.create(
        team=manager.team, season="2026/27", gameweek=10, chip_name="freehit", bank=0
    )
    GameweekData.objects.create(
        team=manager.team, season="2026/27", gameweek=9, bank=10
    )
    engine = DecisionEngine(manager, api).load()
    assert 8 in engine.public_ids and 18 not in engine.public_ids
    assert engine.inputs()["bank"] == 10
