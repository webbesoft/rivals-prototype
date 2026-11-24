from unittest.mock import patch

import pytest
from django.template.loader import render_to_string

from rivals.forms import DecisionInputForm
from rivals.services.decision_engine import DecisionEngine
from tests.unit.test_decision_engine import api, confirmed, form_data, isolated, manager


@pytest.mark.parametrize("outgoing", [1, 3, 8, 13])
def test_buy_choices_match_each_selected_sell_position(manager, api, outgoing):
    engine = DecisionEngine(manager, api).load()
    data = form_data(engine)
    data.update(out_1=outgoing, out_2=8)
    form = DecisionInputForm(data, engine=engine)

    for index, pid in ((1, outgoing), (2, 8)):
        choices = {value for value, _ in form.fields[f"in_{index}"].choices if value}
        assert choices == {
            player_id
            for player_id, player in engine.players.items()
            if player["element_type"] == engine.players[pid]["element_type"]
        }


def test_buy_choices_are_empty_until_a_sell_player_is_selected(manager, api):
    engine = DecisionEngine(manager, api).load()
    form = DecisionInputForm(engine=engine)

    for index in (1, 2):
        assert list(form.fields[f"in_{index}"].choices) == [("", "No move")]


def test_saved_routes_keep_their_buy_selection_and_matching_choices(manager, api):
    engine = DecisionEngine(manager, api).load()
    inputs = confirmed(engine, manual=[[8, 18], [3, 17]])
    with patch.object(engine, "inputs", return_value=inputs):
        form = DecisionInputForm(engine=engine)

    for index, outgoing, incoming in ((1, 8, 18), (2, 3, 17)):
        assert form[f"out_{index}"].value() == outgoing
        assert form[f"in_{index}"].value() == incoming
        assert all(
            engine.players[pid]["element_type"]
            == engine.players[outgoing]["element_type"]
            for pid, _ in form.fields[f"in_{index}"].choices
            if pid
        )


@pytest.mark.parametrize("incoming,valid", [(18, True), (17, False)])
def test_submitted_buys_require_the_same_position(manager, api, incoming, valid):
    engine = DecisionEngine(manager, api).load()
    data = form_data(engine)
    data.update(out_1=8, in_1=incoming)
    form = DecisionInputForm(data, engine=engine)

    assert form.is_valid() is valid
    if not valid:
        assert "in_1" in form.errors


@pytest.mark.parametrize("outgoing", ["", "unknown", "9999"])
def test_invalid_sell_values_do_not_offer_buys(manager, api, outgoing):
    engine = DecisionEngine(manager, api).load()
    data = form_data(engine)
    data.update(out_1=outgoing)
    form = DecisionInputForm(data, engine=engine)

    assert list(form.fields["in_1"].choices) == [("", "No move")]


def test_editor_includes_the_player_catalog_for_changing_sell_positions(manager, api):
    engine = DecisionEngine(manager, api).load()
    form = DecisionInputForm(engine=engine)
    html = render_to_string("partials/decision_form.html", {"form": form})

    assert "data-transfer-route" in html
    assert 'id="transfer-route-players" type="application/json"' in html
    assert {player["position"] for player in form.transfer_players} == {1, 2, 3, 4}


@pytest.mark.parametrize("model", ["heuristic-v1", "decision-v12-calibrated"])
@pytest.mark.parametrize("horizon", [3, 6])
def test_route_labels_include_clubs_and_the_active_forecast_difference(
    manager, api, settings, model, horizon
):
    settings.FPL_PLAYER_MODEL = model
    engine = DecisionEngine(manager, api).load()
    engine.prepare(confirmed(engine, horizon=horizon))
    data = form_data(engine)
    data.update(out_1=8, in_1=18, horizon=horizon)
    form = DecisionInputForm(data, engine=engine)

    assert dict(form.fields["out_1"].choices)[8] == "Player 8 · Club 3"
    difference = round(
        sum(engine.projections[18].values()) - sum(engine.projections[8].values()), 1
    )
    assert dict(form.fields["in_1"].choices)[18] == (
        f"Player 18 · Club 6 · {difference:+.1f} xPts"
    )
    assert dict(form.fields["in_1"].choices)[8] == "Player 8 · Club 3 · +0.0 xPts"
    catalog = {player["id"]: player for player in form.transfer_players}
    assert sum(catalog[18]["points"][:horizon]) == pytest.approx(
        sum(engine.projections[18].values())
    )
    assert len(engine.weeks) == horizon
    html = render_to_string("partials/decision_form.html", {"form": form})
    assert f"GW11–{10 + horizon}" in html
    assert "before captaincy and transfer hits" in html


def test_route_forecasts_do_not_change_the_engine_prepared_horizon(manager, api):
    engine = DecisionEngine(manager, api).load()
    engine.prepare(confirmed(engine, horizon=3))
    projections = engine.projections
    form = DecisionInputForm(engine=engine)

    assert engine.weeks == [11, 12, 13]
    assert engine.projections is projections
    assert all(len(player["points"]) == 6 for player in form.transfer_players)


def test_route_forecasts_stop_at_gameweek_38(manager, api):
    engine = DecisionEngine(manager, api).load()
    engine.context["decision_gameweek"] = 38
    form = DecisionInputForm(engine=engine)

    assert form.transfer_start_week == form.transfer_end_week == 38
    assert all(len(player["points"]) == 1 for player in form.transfer_players)


def test_invalid_horizon_keeps_route_labels_renderable(manager, api):
    engine = DecisionEngine(manager, api).load()
    data = form_data(engine)
    data.update(horizon="invalid", out_1=8)
    form = DecisionInputForm(data, engine=engine)

    assert form.transfer_end_week == 15
    assert "Club 6" in dict(form.fields["in_1"].choices)[18]
    assert not form.is_valid()
    assert "horizon" in form.errors
