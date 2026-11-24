"""Prospective experiments: capture before deadlines, score only final results."""

import math
from copy import deepcopy

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from rivals.models import GameweekForecast
from .component_forecast import ComponentForecast
from .calibrated_component_forecast import (
    CalibratedComponentForecast,
    identity_parameters,
    load_parameters,
    shadow_fingerprint,
)
from .forecast_evaluation import evaluate_models, select_xi, top_projected
from .gameweek_context_service import GameweekContextService
from .player_forecast import PlayerForecast, number
from .forecast_parameters import COMPONENT_CONFIG, model_fingerprint
from .forecast_promotion_policy import promotion_policy
from .forecast_diagnostics import groups_for_frame, coverage

EXPERIMENT_VERSION = "forecast-experiment-v4"
FIXTURE_CONTROL = "decision-v12-fixture-only"


def forecasts(frame):
    players, fixtures = frame["players"], frame["fixtures"]
    scoring, week = frame["scoring_gameweek"], frame["gameweek"]
    histories = {int(k): v for k, v in frame.get("histories", {}).items()}
    current = PlayerForecast(players, fixtures, scoring)
    challenger = ComponentForecast(
        players,
        fixtures,
        scoring,
        teams=frame.get("teams", []),
        histories=histories,
        season=frame["season"],
    )
    result = {
        current.version: {p["id"]: current.points(p["id"], week) for p in players},
        challenger.version: {
            p["id"]: challenger.points(p["id"], week) for p in players
        },
        "ppg": {p["id"]: max(0, number(p.get("points_per_game"))) for p in players},
        "form": {p["id"]: max(0, number(p.get("form"))) for p in players},
    }
    ep = {p["id"]: number(p.get("ep_next"), float("nan")) for p in players}
    ep = {pid: value for pid, value in ep.items() if math.isfinite(value)}
    if ep:
        result["ep_next"] = ep
    if frame.get("revised_parameters") is not None:
        parameters = frame["revised_parameters"]
        if any(
            int(s[:4]) >= int(frame["season"][:4])
            for s in parameters.get("training_seasons", [])
        ):
            raise ValueError("Shadow calibration must be fitted on earlier seasons")
        for name, config in (
            (CalibratedComponentForecast.version, parameters),
            (FIXTURE_CONTROL, identity_parameters(parameters["strength_power"])),
        ):
            revised = CalibratedComponentForecast(
                players,
                fixtures,
                scoring,
                teams=frame.get("teams", []),
                histories=histories,
                season=frame["season"],
                parameters=config,
            )
            result[name] = {p["id"]: revised.points(p["id"], week) for p in players}
    return result


def prepare_forecast(bootstrap, fixtures, *, captured_at=None, histories=None):
    from .active_player_forecast import active_model_name

    now = captured_at or timezone.now()
    context = GameweekContextService.derive(bootstrap.get("events", []), now=now)
    week = context["decision_gameweek"]
    if not context["season"] or not week:
        return None
    clubs = {team["id"] for team in bootstrap.get("teams", [])}
    players = [
        p
        for p in bootstrap.get("elements", [])
        if p.get("element_type") in (1, 2, 3, 4)
        and not p.get("removed", False)
        and (not clubs or p.get("team") in clubs)
    ]
    if not players:
        return None
    frame = {
        "season": context["season"],
        "gameweek": week,
        "captured_at": now.isoformat(),
        "deadline": context["next_deadline"].isoformat(),
        "scoring_gameweek": context["scoring_gameweek"] or 0,
        "players": players,
        "bootstrap": bootstrap,
        "model_versions": {
            "default_player_model": active_model_name(),
            "reference_player_model": PlayerForecast.version,
            "shadow": CalibratedComponentForecast.version,
            "comparison": ComponentForecast.version,
            "fixture_control": FIXTURE_CONTROL,
        },
        "revised_parameters": deepcopy(load_parameters()),
        "fixtures": fixtures,
        "teams": bootstrap.get("teams", []),
        "histories": histories or {},
        "provenance": "worker-pre-deadline",
    }
    predictions = forecasts(frame)
    selections = {
        model: select_xi(players, values) for model, values in predictions.items()
    }
    if not selections[PlayerForecast.version]:
        return None
    # Keep the challenger breakdown and explicit fallback provenance for audit.
    challenger = ComponentForecast(
        players,
        fixtures,
        frame["scoring_gameweek"],
        teams=frame["teams"],
        histories={int(k): v for k, v in frame["histories"].items()},
        season=frame["season"],
    )
    frame["challenger_details"] = {
        p["id"]: challenger.components(p["id"], week) for p in players
    }
    frame["challenger_details_by_model"] = {
        ComponentForecast.version: frame["challenger_details"]
    }
    for name, parameters in (
        (CalibratedComponentForecast.version, frame["revised_parameters"]),
        (
            FIXTURE_CONTROL,
            identity_parameters(frame["revised_parameters"]["strength_power"]),
        ),
    ):
        revised = CalibratedComponentForecast(
            players,
            fixtures,
            frame["scoring_gameweek"],
            teams=frame["teams"],
            histories={int(k): v for k, v in frame["histories"].items()},
            season=frame["season"],
            parameters=parameters,
        )
        frame["challenger_details_by_model"][name] = {
            p["id"]: revised.components(p["id"], week) for p in players
        }
    frame["model_config"] = dict(COMPONENT_CONFIG)
    frame["model_fingerprint"] = shadow_fingerprint(frame["revised_parameters"])
    frame["comparison_fingerprint"] = model_fingerprint()
    frame["promotion_policy"] = promotion_policy(EXPERIMENT_VERSION)
    frame["history_usage"] = {
        pid: {
            "has_usable_history": bool(challenger.histories.get(pid)),
            "usable_matches": len(challenger.histories.get(pid, [])),
        }
        for pid in challenger.players
    }
    groups = groups_for_frame(players, fixtures, week, frame["histories"])
    frame["history_coverage"] = coverage(groups, challenger.players)
    k = frame["promotion_policy"]["primary_top_k"]
    frame["candidate_history_coverage"] = {
        name: coverage(groups, top_projected(values, k))
        for name, values in predictions.items()
    }
    baseline, shadow = (
        predictions[PlayerForecast.version],
        predictions[CalibratedComponentForecast.version],
    )
    common = set(baseline) & set(shadow)
    union = top_projected({i: baseline[i] for i in common}, k) | top_projected(
        {i: shadow[i] for i in common}, k
    )
    frame["common_candidate_history_coverage"] = coverage(groups, union)

    return {"frame": frame, "predictions": predictions, "selections": selections}


class ForecastDeadlinePassed(Exception):
    """Roll back a forecast whose write waited past the publication deadline."""


def publish_forecast(prepared, *, publication_guard=None):
    if not prepared:
        return False
    frame = prepared["frame"]
    captured, deadline = parse_datetime(frame["captured_at"]), parse_datetime(
        frame["deadline"]
    )
    if captured >= deadline:
        return False
    identity = {
        "season": frame["season"],
        "gameweek": frame["gameweek"],
        "version": EXPERIMENT_VERSION,
    }
    values = {
        "captured_at": captured,
        "deadline": deadline,
        "inputs": frame,
        "predictions": prepared["predictions"],
        "selections": prepared["selections"],
    }
    try:
        # Outermost publication: a caller cannot postpone this commit in its own
        # transaction. Recheck after lock acquisition/writes, immediately before
        # committing; commit I/O itself cannot have a wall-clock upper bound.
        with transaction.atomic(durable=True):
            if publication_guard:
                publication_guard()
            if timezone.now() >= deadline:
                return False
            record, created = GameweekForecast.objects.get_or_create(
                **identity, defaults=values
            )
            record = GameweekForecast.objects.select_for_update().get(pk=record.pk)
            if timezone.now() >= deadline:
                raise ForecastDeadlinePassed
            if not created:
                if record.outcomes or record.captured_at >= captured:
                    return False
                GameweekForecast.objects.filter(
                    pk=record.pk, outcomes={}, captured_at__lt=captured
                ).update(**values)
            if timezone.now() >= deadline:
                raise ForecastDeadlinePassed
    except ForecastDeadlinePassed:
        return False
    return True


def review_forecasts(api, *, publication_guard=None):
    bootstrap = api.fetch_bootstrap_static()
    if not bootstrap:
        return 0
    context = GameweekContextService.derive(bootstrap.get("events", []))
    finished = {
        e["id"]
        for e in bootstrap.get("events", [])
        if e.get("finished") and e.get("data_checked")
    }
    count = 0
    for record in GameweekForecast.objects.filter(
        season=context["season"], outcomes={}, gameweek__in=finished
    ):
        payload = api.fetch_gameweek_live(
            record.gameweek, record.season, force_refresh=True
        )
        actual = {
            p["id"]: p.get("stats", {}).get("total_points")
            for p in (payload or {}).get("elements", [])
        }
        actual = {
            pid: number(value)
            for pid, value in actual.items()
            if math.isfinite(number(value, float("nan")))
        }
        cs_labels = {
            p["id"]: (
                number(p["stats"]["clean_sheets"])
                if number(p["stats"].get("minutes")) >= 60
                else 0.0
            )
            for p in (payload or {}).get("elements", [])
            if math.isfinite(
                number(p.get("stats", {}).get("clean_sheets"), float("nan"))
            )
            and math.isfinite(number(p.get("stats", {}).get("minutes"), float("nan")))
        }
        try:
            evaluated = evaluate_models(
                record.inputs["players"],
                {
                    model: {int(k): v for k, v in values.items()}
                    for model, values in record.predictions.items()
                },
                actual,
                selections=record.selections,
                fixtures=record.inputs["fixtures"],
                week=record.gameweek,
                frame={**record.inputs, "actual_clean_sheets": cs_labels},
            )
        except ValueError:
            continue
        record.outcomes = {
            "actual": actual,
            "metrics": evaluated["metrics"],
            "paired_differences": evaluated["paired_differences"],
            "paired_history_differences": evaluated["paired_history_differences"],
            "actual_clean_sheets": cs_labels,
            "scored_at": timezone.now().isoformat(),
            "comparison": "same pre-deadline pool; unrestricted budget; captain doubled",
        }
        with transaction.atomic():
            if publication_guard:
                publication_guard()
            # A replacement worker (or another scorer) may have completed this
            # record while the transport/model work ran outside the transaction.
            count += GameweekForecast.objects.filter(pk=record.pk, outcomes={}).update(
                outcomes=record.outcomes
            )
    return count
