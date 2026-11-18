from collections import Counter
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.urls import reverse

from rivals.models import TeamPlanningInput
from rivals.services.decision_engine import DecisionEngine
from tests.unit.test_decision_engine import (
    api,
    manager,
    confirmed,
    isolated,
    initial_payload,
    NOW,
)


def planned():
    return {"xi": [1, 3, 4, 5, 8, 9, 10, 11, 13, 14, 15], "captain": 15, "vice": 13}


def lineup_post(lineup=None, **changes):
    lineup = lineup or planned()
    return {
        "season": "2026/27",
        "gameweek": 11,
        "mode": "planned",
        "starters": lineup["xi"],
        "captain": lineup["captain"],
        "vice": lineup["vice"],
        **changes,
    }


@pytest.mark.django_db
def test_planned_xi_replaces_doku_slot_and_keeps_three_defenders(manager, api):
    engine = DecisionEngine(manager, api).load()
    engine.prepare(confirmed(engine, lineup=planned()))
    # Tempt the optimiser to choose the two deliberately benched defenders.
    for pid in (6, 7):
        engine.projections[pid][11] = 100
    baseline = engine.evaluate(engine.ids)
    choice = engine.candidate([(8, 18)], baseline)
    first = baseline["lineups"][0]
    after = choice["lineups"][0]
    assert set(first["xi"]) == set(planned()["xi"])
    assert set(after["xi"]) == set(planned()["xi"]) - {8} | {18}
    assert Counter(engine.players[pid]["element_type"] for pid in after["xi"])[2] == 3
    assert first["captain"] == after["captain"] == 15
    assert choice["immediate"]["net_gain"] == pytest.approx(
        engine.projections[18][11] - engine.projections[8][11]
    )
    assert first["selection"] == "planned"
    assert baseline["lineups"][1]["selection"] == "optimised"
    assert (
        baseline["lineups"][1] == engine.evaluate(engine.ids, lineup={})["lineups"][1]
    )
    # A rival with exactly the same squad still gets an independent optimised XI.
    rival = engine.evaluate(engine.ids, lineup={})
    assert 6 in rival["lineups"][0]["xi"] and 7 in rival["lineups"][0]["xi"]
    assert rival["lineups"][0]["selection"] == "optimised"


@pytest.mark.django_db
def test_bench_purchase_stays_benched_and_captain_transfer_inherits_armband(
    manager, api
):
    engine = DecisionEngine(manager, api).load()
    engine.prepare(confirmed(engine, lineup=planned(), bank=50))
    baseline = engine.evaluate(engine.ids)
    bench = engine.candidate([(12, 18)], baseline)
    assert bench["immediate"]["net_gain"] == 0
    assert 18 not in bench["lineups"][0]["xi"]
    captain = engine.candidate([(15, 19)], baseline)
    assert captain["lineups"][0]["captain"] == 19
    assert 15 not in captain["lineups"][0]["xi"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "change",
    [
        {"xi": [1, 3, 4, 8, 9, 10, 11, 12, 13, 14, 15]},
        {"xi": [1, 3, 3, 5, 8, 9, 10, 11, 13, 14, 15]},
        {"xi": [1, 3, 4, 5, 8, 9, 10, 11, 13, 19, 15]},
        {"captain": 12},
        {"vice": 15},
    ],
)
def test_illegal_lineups_are_rejected(manager, api, change):
    engine = DecisionEngine(manager, api).load()
    with pytest.raises(ValueError):
        engine.prepare(confirmed(engine, lineup={**planned(), **change}))


@pytest.mark.django_db
def test_lineup_endpoint_persists_private_choices_and_refreshes_all_comparisons(
    manager, api, client, settings
):
    settings.PASSWORDLESS_AUTH_ENABLED = False
    client.force_login(manager)
    with patch("rivals.services.decision_engine.FplApiService", return_value=api):
        before = DecisionEngine(manager, api).report()
        response = client.post(reverse("rivals:decisions.lineup"), lineup_post())
        assert response.status_code == 200
        assert TeamPlanningInput.objects.get(user=manager).planned_lineup == planned()
        after = DecisionEngine(manager, api).report()
        assert before["fingerprint"] != after["fingerprint"]
        assert set(after["baseline"]["lineups"][0]["xi"]) == set(planned()["xi"])
        assert after["captain_id"] == 15
        assert after["inputs"]["lineup"] == planned()
        html = response.content.decode()
        assert "Your planned XI" in html
        assert html.index('id="planner-pitch-heading"') < html.index(
            "Before the next deadline"
        )
        assert html.count('id="planner-pitch-heading"') == 1
        for choice in after["options"]:
            assert choice["weekly"][0]["points"] == pytest.approx(
                choice["immediate"]["points"], abs=0.1
            )
        response = client.post(
            reverse("rivals:decisions.lineup"), lineup_post(mode="optimised")
        )
        assert response.status_code == 200
        assert TeamPlanningInput.objects.get(user=manager).planned_lineup == {}


@pytest.mark.django_db
def test_invalid_submission_preserves_saved_lineup_and_financial_anchors(
    manager, api, client, settings
):
    settings.PASSWORDLESS_AUTH_ENABLED = False
    client.force_login(manager)
    record = TeamPlanningInput.objects.create(
        user=manager,
        team=manager.team,
        season="2026/27",
        gameweek=11,
        planned_lineup=planned(),
        bank="1.2",
        free_transfers=3,
    )
    with patch("rivals.services.decision_engine.FplApiService", return_value=api):
        response = client.post(
            reverse("rivals:decisions.lineup"), lineup_post(starters=[1, 3, 4])
        )
        assert (
            "You selected 3 starters. Select exactly 11: add 8 players."
            in response.content.decode()
        )
        record.refresh_from_db()
        assert record.planned_lineup == planned()
        assert float(record.bank) == 1.2 and record.free_transfers == 3
        response = client.post(
            reverse("rivals:decisions.lineup"), lineup_post(gameweek=10)
        )
        assert "deadline has changed" in response.content.decode()


@pytest.mark.django_db
def test_twelve_starters_explains_error_visibly_without_saving(
    manager, api, client, settings
):
    settings.PASSWORDLESS_AUTH_ENABLED = False
    client.force_login(manager)
    with patch("rivals.services.decision_engine.FplApiService", return_value=api):
        response = client.post(
            reverse("rivals:decisions.lineup"),
            lineup_post(starters=planned()["xi"] + [12]),
        )
    html = response.content.decode()
    assert "You selected 12 starters. Select exactly 11: remove 1 player." in html
    assert (
        'class="planner-lineup-error" data-lineup-error role="alert" tabindex="-1" >'
        in html
    )
    assert "Lineup not saved" in html and "Your saved lineup is unchanged" in html
    assert not TeamPlanningInput.objects.filter(user=manager).exists()


@pytest.mark.django_db
def test_visual_editor_has_fifteen_selectable_players_only_for_next_week(
    manager, api, client, settings
):
    settings.PASSWORDLESS_AUTH_ENABLED = False
    client.force_login(manager)
    with patch("rivals.services.decision_engine.FplApiService", return_value=api):
        response = client.get(reverse("rivals:decisions.preview"))
    html = response.content.decode()
    assert html.count("data-lineup-player=") == 15
    assert html.count("data-lineup-select aria-pressed=") == 15
    assert html.count("data-lineup-pitch-editor") == 1
    assert 'type="checkbox"' not in html
    assert 'form="planned-lineup-form"' in html


@pytest.mark.django_db
def test_planned_lineup_is_deadline_scoped_and_requires_post_and_csrf(
    manager, api, client
):
    TeamPlanningInput.objects.create(
        user=manager,
        team=manager.team,
        season="2026/27",
        gameweek=10,
        planned_lineup=planned(),
    )
    engine = DecisionEngine(manager, api).load()
    assert engine.inputs()["lineup"] == {}
    client.force_login(manager)
    assert client.get(reverse("rivals:decisions.lineup")).status_code == 405
    from django.test import Client

    secure = Client(enforce_csrf_checks=True)
    secure.force_login(manager)
    assert (
        secure.post(reverse("rivals:decisions.lineup"), lineup_post()).status_code
        == 403
    )


@pytest.mark.django_db
def test_preview_places_pitch_before_transfer_comparison(
    manager, api, client, settings
):
    settings.PASSWORDLESS_AUTH_ENABLED = False
    client.force_login(manager)
    with patch("rivals.services.decision_engine.FplApiService", return_value=api):
        response = client.get(reverse("rivals:decisions.preview"))
    html = response.content.decode()
    assert html.index('id="planner-pitch-heading"') < html.index(
        'id="decision-results"'
    )
    assert "Edit XI and captain" in html
    assert "can differ from your FPL selection" in html


@pytest.mark.django_db
def test_correcting_owned_player_preserves_lineup_slot_and_refreshes_editor(
    manager, api, client, settings
):
    settings.PASSWORDLESS_AUTH_ENABLED = False
    client.force_login(manager)
    TeamPlanningInput.objects.create(
        user=manager,
        team=manager.team,
        season="2026/27",
        gameweek=11,
        squad_ids=list(range(1, 16)),
        planned_lineup=planned(),
    )
    with patch("rivals.services.decision_engine.FplApiService", return_value=api):
        form = client.get(reverse("rivals:decisions.workspace")).context["form"]
        response = client.post(
            reverse("rivals:decisions.workspace"),
            {
                **initial_payload(form),
                "owned_7": 18,
                "sell_7": "5.5",
            },
        )
    report = response.context["report"]
    assert report["inputs"]["lineup"]["xi"] == [
        18 if pid == 8 else pid for pid in planned()["xi"]
    ]
    editor = response.context["lineup_form"]
    choices = dict(editor.fields["starters"].choices)
    assert 18 in choices and 8 not in choices
    html = response.content.decode()
    assert html.count("data-lineup-pitch-editor") == 1
    assert 'data-lineup-player="18"' in html and 'data-lineup-player="8"' not in html
    assert (
        TeamPlanningInput.objects.get(user=manager).planned_lineup
        == report["inputs"]["lineup"]
    )


@pytest.mark.django_db
@pytest.mark.parametrize("mode,valid", [("optimised", True), ("planned", False)])
def test_optimised_mode_ignores_stale_choices_before_field_validation(
    manager, api, mode, valid
):
    from rivals.forms import LineupInputForm

    engine = DecisionEngine(manager, api).load()
    preview = engine.preview()
    form = LineupInputForm(
        lineup_post(mode=mode, starters=[99999], captain="stale", vice=99999),
        engine=engine,
        initial_lineup=preview["baseline"]["lineups"][0],
    )
    assert form.is_valid() is valid
    if valid:
        assert form.cleaned_data["lineup"] == {}
    else:
        assert all(name in form.errors for name in ("starters", "captain", "vice"))
    expired = LineupInputForm(
        lineup_post(
            mode="optimised", starters=[99999], captain="stale", vice=99999, gameweek=10
        ),
        engine=engine,
        initial_lineup=preview["baseline"]["lineups"][0],
    )
    assert not expired.is_valid()
    assert "deadline has changed" in str(expired.errors)


@pytest.mark.django_db
def test_stale_saved_xi_falls_back_but_submitted_lineups_stay_strict(
    manager, api, client, settings
):
    settings.PASSWORDLESS_AUTH_ENABLED = False
    client.force_login(manager)
    stale = {**planned(), "xi": [99999 if pid == 8 else pid for pid in planned()["xi"]]}
    row = TeamPlanningInput.objects.create(
        user=manager,
        team=manager.team,
        season="2026/27",
        gameweek=11,
        planned_lineup=stale,
    )
    engine = DecisionEngine(manager, api).load()
    preview = engine.preview()
    assert preview["lineup_mode"] == "optimised"
    assert "saved XI could not be applied" in preview["lineup_warning"]
    with pytest.raises(ValueError):
        engine.report(confirmed(engine, lineup=stale))
    with patch("rivals.services.decision_engine.FplApiService", return_value=api):
        response = client.post(
            reverse("rivals:decisions.lineup"),
            lineup_post(
                mode="optimised", starters=[99999], captain="stale", vice=99999
            ),
        )
    assert response.status_code == 200
    row.refresh_from_db()
    assert row.planned_lineup == {}


@pytest.mark.django_db
def test_lineup_creation_inherits_targets_and_existing_rows_keep_targets(
    manager, api, client, settings
):
    from rivals.models import MiniLeague, Team
    from rivals.views.decision_views import planning_record

    settings.PASSWORDLESS_AUTH_ENABLED = False
    client.force_login(manager)
    league = MiniLeague.objects.create(fpl_league_id="2000", name="Target league")
    rival = Team.objects.create(fpl_team_id="2001", team_name="Target rival")
    TeamPlanningInput.objects.create(
        user=manager,
        team=manager.team,
        season="2026/27",
        gameweek=10,
        target_league=league,
        target_rival=rival,
        horizon=4,
        strategy="chase",
    )
    with patch("rivals.services.decision_engine.FplApiService", return_value=api):
        response = client.post(reverse("rivals:decisions.lineup"), lineup_post())
    assert response.status_code == 200
    row = TeamPlanningInput.objects.get(user=manager, gameweek=11)
    assert (row.target_league_id, row.target_rival_id, row.horizon, row.strategy) == (
        league.pk,
        rival.pk,
        4,
        "chase",
    )
    engine = DecisionEngine(manager, api).load()
    planning_record(engine, {**engine.inputs(), "league_id": None, "rival_id": None})
    row.refresh_from_db()
    assert (row.target_league_id, row.target_rival_id) == (league.pk, rival.pk)


@pytest.mark.django_db
@pytest.mark.parametrize("explicit", [False, True])
def test_lineup_only_save_preserves_legacy_financial_anchors(
    manager, api, client, settings, explicit
):
    settings.PASSWORDLESS_AUTH_ENABLED = False
    client.force_login(manager)
    old = NOW - timedelta(days=2)
    bank_date = old + timedelta(hours=1) if explicit else None
    row = TeamPlanningInput.objects.create(
        user=manager,
        team=manager.team,
        season="2026/27",
        gameweek=11,
        bank="1.2",
        free_transfers=3,
        bank_recorded_at=bank_date,
    )
    TeamPlanningInput.objects.filter(pk=row.pk).update(updated_at=old)
    before = DecisionEngine(manager, api).load().inputs()
    with patch("rivals.services.decision_engine.FplApiService", return_value=api):
        response = client.post(reverse("rivals:decisions.lineup"), lineup_post())
    assert response.status_code == 200
    row.refresh_from_db()
    assert row.bank_recorded_at == (bank_date or old)
    assert row.free_transfers_recorded_at == old
    assert float(row.bank) == 1.2 and row.free_transfers == 3
    assert row.updated_at > old
    after = DecisionEngine(manager, api).load().inputs()
    assert (before["bank"], before["free_transfers"]) == (
        after["bank"],
        after["free_transfers"],
    )


@pytest.mark.django_db
def test_reordering_retained_players_does_not_change_their_lineup_roles(
    manager, api, client, settings
):
    settings.PASSWORDLESS_AUTH_ENABLED = False
    client.force_login(manager)
    lineup = {**planned(), "captain": 8}
    row = TeamPlanningInput.objects.create(
        user=manager,
        team=manager.team,
        season="2026/27",
        gameweek=11,
        squad_ids=list(range(1, 16)),
        planned_lineup=lineup,
    )
    with patch("rivals.services.decision_engine.FplApiService", return_value=api):
        form = client.get(reverse("rivals:decisions.workspace")).context["form"]
        response = client.post(
            reverse("rivals:decisions.workspace"),
            {**initial_payload(form), "owned_7": 12, "owned_11": 8},
        )
    assert response.context["report"] is not None
    row.refresh_from_db()
    assert row.planned_lineup == lineup


@pytest.mark.django_db
@pytest.mark.parametrize("replace_player", [False, True])
def test_workspace_keeps_a_lineup_saved_during_forecasting(
    manager, api, client, settings, replace_player
):
    settings.PASSWORDLESS_AUTH_ENABLED = False
    client.force_login(manager)
    row = TeamPlanningInput.objects.create(
        user=manager,
        team=manager.team,
        season="2026/27",
        gameweek=11,
        squad_ids=list(range(1, 16)),
        planned_lineup=planned(),
    )
    newer = {**planned(), "captain": 13, "vice": 15}
    real_report = DecisionEngine.report
    changed = False

    def concurrent_report(engine, inputs=None, **kwargs):
        nonlocal changed
        result = real_report(engine, inputs, **kwargs)
        if inputs is not None and not changed:
            changed = True
            TeamPlanningInput.objects.filter(pk=row.pk).update(planned_lineup=newer)
        return result

    with patch("rivals.services.decision_engine.FplApiService", return_value=api):
        form = client.get(reverse("rivals:decisions.workspace")).context["form"]
        payload = {**initial_payload(form), "bank": "2.0"}
        if replace_player:
            payload.update(owned_7=18, sell_7="5.5")
        with patch.object(DecisionEngine, "report", concurrent_report):
            response = client.post(reverse("rivals:decisions.workspace"), payload)
    row.refresh_from_db()
    expected = {
        **newer,
        "xi": [18 if pid == 8 and replace_player else pid for pid in newer["xi"]],
    }
    assert row.planned_lineup == expected
    assert response.context["report"]["inputs"]["lineup"] == expected
    assert float(row.bank) == 2.0


@pytest.mark.django_db
@pytest.mark.parametrize("endpoint", ["decisions.lineup", "decisions.workspace"])
def test_conditional_saves_reject_a_concurrent_squad_change(
    manager, api, client, settings, endpoint
):
    from rivals.views.decision_views import update_planning_record

    settings.PASSWORDLESS_AUTH_ENABLED = False
    client.force_login(manager)
    row = TeamPlanningInput.objects.create(
        user=manager,
        team=manager.team,
        season="2026/27",
        gameweek=11,
        squad_ids=list(range(1, 16)),
        planned_lineup=planned(),
    )
    new_ids = [18 if pid == 8 else pid for pid in row.squad_ids]
    new_lineup = {
        **planned(),
        "xi": [18 if pid == 8 else pid for pid in planned()["xi"]],
    }

    def concurrent_write(record, values):
        TeamPlanningInput.objects.filter(pk=row.pk).update(
            squad_ids=new_ids, planned_lineup=new_lineup
        )
        return update_planning_record(record, values)

    with patch("rivals.services.decision_engine.FplApiService", return_value=api):
        form = client.get(reverse("rivals:decisions.workspace")).context["form"]
        payload = (
            lineup_post(captain=13, vice=15)
            if endpoint == "decisions.lineup"
            else {**initial_payload(form), "bank": "2.0"}
        )
        with patch(
            "rivals.views.decision_views.update_planning_record", concurrent_write
        ):
            response = client.post(reverse(f"rivals:{endpoint}"), payload)
    row.refresh_from_db()
    assert row.planned_lineup == new_lineup and row.squad_ids == new_ids
    assert row.bank is None
    html = response.content.decode()
    assert "changed while this plan was being saved" in html
    assert 'data-lineup-player="18"' in html and 'data-lineup-player="8"' not in html


@pytest.mark.django_db
def test_results_get_omits_pitch_while_preview_contains_one(
    manager, api, client, settings
):
    settings.PASSWORDLESS_AUTH_ENABLED = False
    client.force_login(manager)
    with patch("rivals.services.decision_engine.FplApiService", return_value=api):
        preview = client.get(reverse("rivals:decisions.preview")).content.decode()
        results = client.get(
            reverse("rivals:decisions.workspace"), {"results": "1"}
        ).content.decode()
    assert preview.count("data-lineup-pitch-editor") == 1
    assert "data-lineup-pitch-editor" not in results
    assert 'id="planner-gw-' not in results
