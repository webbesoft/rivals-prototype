"""Review regressions exercised through stored delivery and real planner saves."""

import re
from copy import deepcopy
from unittest.mock import patch

import pytest
from django.core import mail
from django.core.cache import cache
from django.template.loader import render_to_string
from django.test import Client
from django.urls import reverse
from django.utils.html import strip_tags

from rivals.models import DecisionRecord
from rivals.services.decision_engine import DecisionEngine
from rivals.services.deadline_digest_service import deliver_digest
from tests.unit.test_deadline_digests import record_for, subscriber
from tests.unit.test_decision_engine import (
    NOW,
    api,
    confirmed,
    forecast_routes,
    initial_payload,
    isolated,
    manager,
)


def comparison_rows(content):
    rows = re.findall(r"<tr\b[^>]*>(?:(?!<tr\b).)*?</tr>", content, re.S)
    return [" ".join(strip_tags(row).split()) for row in rows]


@pytest.mark.parametrize("hold", [False, True])
def test_fresh_legacy_report_delivers_both_points_comparisons(
    subscriber, api, settings, hold
):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.SITE_URL = "https://rivalspy.example"
    if hold:
        for player in api.fetch_bootstrap_static.return_value["elements"][15:]:
            player.update(form="0", points_per_game="0")
    report = DecisionEngine(subscriber, api).report()
    legacy = deepcopy(report)
    for key in (
        "end_gameweek",
        "short_term",
        "short_term_is_separate",
        "player_outlooks",
    ):
        legacy.pop(key)
    for option in [legacy["baseline"], legacy["best"]] + legacy["options"]:
        for key in ("immediate", "net_points", "weekly"):
            option.pop(key, None)
    record = record_for(
        subscriber, report=legacy, prepared_at=NOW, generation_state="ready"
    )
    record.refresh_from_db()
    frozen_evidence = deepcopy(record.report)
    with patch(
        "rivals.services.deadline_digest_service.FplApiService", return_value=api
    ):
        deliver_digest.now(str(record.pk))
    record.refresh_from_db()
    assert record.delivery_state == "sent" and len(mail.outbox) == 1
    message = mail.outbox[0]
    rows = comparison_rows(message.alternatives[0].content)
    first_hold = legacy["baseline"]["lineups"][0]["points"]
    first_plan = legacy["best"]["lineups"][0]["points"] - legacy["best"]["hits"]
    window_plan = legacy["best"]["points"] - legacy["best"]["hits"]
    gain = first_plan - first_hold
    first_gain = f"{gain:+.1f}" if gain > 0 else f"{gain:.1f}"
    assert any(
        "GW11This gameweek" in row
        and row.endswith(f"{first_hold:.1f} {first_plan:.1f} {first_gain}")
        for row in rows
    )
    window_gain = legacy["best"]["net_gain"]
    formatted_window_gain = (
        f"{window_gain:+.1f}" if window_gain > 0 else f"{window_gain:.1f}"
    )
    assert any(
        "GW11–155 gameweeks total" in row
        and row.endswith(
            f'{legacy["baseline"]["points"]:.1f} {window_plan:.1f} {formatted_window_gain}'
        )
        for row in rows
    )
    action = "hold / roll" if hold else "transfer"
    assert (
        f"GW11 (this gameweek): hold {first_hold:.1f}; {action} {first_plan:.1f}; gain vs hold {first_gain} pts."
        in message.body
    )
    assert "Best for GW11" not in message.body
    assert (
        record.report == frozen_evidence
    )  # Presentation must not rewrite persisted JSON evidence.


@pytest.mark.django_db
@pytest.mark.parametrize("manual", [False, True])
def test_separate_immediate_route_saves_through_real_report_path(manager, api, manual):
    for player in api.fetch_bootstrap_static.return_value["elements"]:
        value = 8 if player["id"] in range(3, 8) else 3 if player["id"] <= 15 else 0
        if player["id"] == 17:
            value = 12
        player.update(form=str(value), points_per_game=str(value))
    api.fetch_fixtures_data.return_value[:] = [
        fixture
        for fixture in api.fetch_fixtures_data.return_value
        if fixture["event"] == 11 or 6 not in (fixture["team_h"], fixture["team_a"])
    ]
    client = Client()
    client.force_login(manager)
    with patch("rivals.services.decision_engine.FplApiService", return_value=api):
        page = client.get(reverse("rivals:decisions.workspace"))
        if manual:
            data = initial_payload(page.context["form"])
            data.update(out_1=1, in_1=16)
            page = client.post(reverse("rivals:decisions.workspace"), data)
            assert page.context["form"].is_valid()
        report = page.context["report"]
        assert report["best"]["key"] == "hold"
        assert report["short_term_is_separate"]
        choice = report["short_term"]
        assert choice["moves"] and choice["save_token"]
        assert choice["key"] not in {option["key"] for option in report["options"]}
        assert f'value="{choice["save_token"]}"' in page.content.decode()
        cache.clear()  # Force the save view to rebuild the report from persisted inputs.
        response = client.post(
            reverse("rivals:decisions.save"), {"choice": choice["save_token"]}
        )
    assert response.status_code == 302
    saved = DecisionRecord.objects.get(user=manager).snapshot["chosen"]
    assert saved["key"] == choice["key"]
    assert saved["moves"] == choice["moves"]


@pytest.mark.django_db
def test_summary_preserves_captaincy_and_charges_four_point_hit_once(manager, api):
    engine = DecisionEngine(manager, api).load()
    engine.prepare(confirmed(engine, free_transfers=0))
    for pid in engine.players:
        engine.projections[pid] = dict(zip(engine.weeks, [50 / 12, 75 / 12, 75 / 12]))
    engine.projections[17] = dict(zip(engine.weeks, [50 / 12 + 2.5, 7.5, 7.5]))
    baseline = engine.evaluate(engine.ids)
    hold = engine.candidate([], baseline)
    route = engine.candidate([(3, 17)], baseline)
    assert route["points"] == 210 and route["lineups"][0]["points"] == 55
    assert route["lineups"][0]["captain"] == 17
    assert route["net_points"] == 206 and route["immediate"]["points"] == 51
    report = {
        "gameweek": 11,
        "end_gameweek": 13,
        "horizon": 3,
        "baseline": hold,
        "best": route,
        "short_term": route,
    }
    rows = comparison_rows(
        render_to_string("partials/decision_summary.html", {"report": report})
    )
    assert any(
        "GW11This gameweek" in row and row.endswith("50.0 51.0 +1.0") for row in rows
    )
    assert any(
        "GW11–133 gameweeks total" in row and row.endswith("200.0 206.0 +6.0")
        for row in rows
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "forecasts",
    [
        {17: [3.005, 3, 3], 21: [3.02, 3, 3]},
        {17: [3.055, 10, 10], 21: [3.07, 3, 3]},
    ],
)
def test_immediate_ranking_does_not_round_away_points(manager, api, forecasts):
    engine = DecisionEngine(manager, api).load()
    forecast_routes(engine, forecasts, [[(3, 17)], [(3, 21)]])
    report = engine.report(confirmed(engine))
    assert report["short_term"]["key"] == "3:21"


@pytest.mark.django_db
def test_immediate_shortlist_skips_illegal_singles_but_keeps_funding_pairs(
    manager, api
):
    engine = DecisionEngine(manager, api).load()
    engine.prepare(confirmed(engine))
    for index, pid in enumerate((18, 22, 26, 30, 34, 38, 42)):
        engine.players[pid]["team"] = 6 if pid == 26 else 1 + index % 2
        values = (
            [20, 0, 0]
            if pid == 18
            else [19, 0, 0] if pid == 22 else [18, 0, 0] if pid == 26 else [8, 50, 50]
        )
        engine.projections[pid] = dict(zip(engine.weeks, values))
    engine.players[18]["now_cost"] = 50
    choices = engine.options(engine.evaluate(engine.ids))
    assert any(choice["key"] == "8:26" for choice in choices)
    assert any(choice["key"] == "1:16,8:18" for choice in choices)


@pytest.mark.parametrize("allowance", [None, 0, 2])
@pytest.mark.parametrize("moves", [[], [{"out_name": "A", "in_name": "B"}]])
def test_allowance_text_distinguishes_unknown_and_zero(allowance, moves):
    report = {
        "gameweek": 11,
        "horizon": 5,
        "end_gameweek": 15,
        "baseline": {"free_next": allowance},
        "best": {
            "key": "best",
            "moves": moves,
            "immediate": {"net_gain": 1},
            "free_next": 1,
        },
        "short_term": {"key": "best"},
    }
    content = strip_tags(
        render_to_string("partials/decision_points.html", {"report": report})
    )
    if allowance is None:
        assert "Next gameweek’s free-transfer allowance is unknown" in content
        assert "None free transfer" not in content
    else:
        assert f"Holding leaves {allowance} free transfer" in content
    assert (
        "the suggested route leaves 1" if moves else "no transfer hit applies"
    ) in content


@pytest.mark.parametrize("gain,expected", [(None, "—"), (0, "0.0"), (0.6, "+0.6")])
def test_alternative_header_marks_unknown_immediate_gain(gain, expected):
    report = {
        "gameweek": 11,
        "alternatives": [{"options": [{"immediate": {"net_gain": gain}}]}],
    }
    content = strip_tags(
        render_to_string("partials/decision_alternatives.html", {"report": report})
    )
    assert f"GW11: {expected} pts" in content


@pytest.mark.parametrize(
    "unknown", [[], ["Confirm selling prices"], ["Confirm your bank balance"]]
)
def test_route_bank_discloses_unknown_finances(unknown):
    option = {
        "moves": [{"out_name": "A", "in_name": "B"}],
        "unknown": unknown,
        "bank_after": None if "Confirm your bank balance" in unknown else 1.2,
    }
    content = strip_tags(
        render_to_string("partials/decision_route.html", {"option": option})
    )
    if "Confirm selling prices" in unknown:
        assert "upper bound" in content and "Confirm selling prices" in content
        assert "£1.2m" in content
    elif unknown:
        assert "Bank estimate unavailable" in content and "£Nonem" not in content
    else:
        assert "Estimated bank after moves: £1.2m" in content
