"""Review frozen pre-deadline forecasts without re-fitting on later results."""

from django.db.models import Q

from rivals.models import DecisionRecord
from rivals.services.decision_engine import DecisionUnavailable
from rivals.services.fpl_api_service import FplApiService
from rivals.services.gameweek_context_service import GameweekContextService


def review_decisions(api=None):
    api = api or FplApiService()
    api.TIMEOUT = 4
    bootstrap = api.fetch_bootstrap_static()
    if not bootstrap:
        raise DecisionUnavailable("Gameweek results are unavailable.")
    context = GameweekContextService.derive(bootstrap.get("events", []))
    if not context["season"]:
        raise DecisionUnavailable("Season details are unavailable.")
    finished = {
        event["id"]
        for event in bootstrap.get("events", [])
        if event.get("finished") and event.get("data_checked")
    }
    live_by_week = {}
    reviewed = 0
    records = DecisionRecord.objects.filter(season=context["season"]).filter(
        Q(outcomes={}) | Q(outcomes__complete=False)
    )
    for record in records.iterator(chunk_size=50):
        snapshot = record.snapshot
        report, chosen = snapshot["report"], snapshot["chosen"]
        weeks = dict(record.outcomes.get("weeks", {}))
        base_lines = {line["gameweek"]: line for line in report["baseline"]["lineups"]}
        selected_lines = {line["gameweek"]: line for line in chosen["lineups"]}
        for week in report["weeks"]:
            if str(week) in weeks or week not in finished:
                continue
            if week not in live_by_week:
                payload = api.fetch_gameweek_live(week, context["season"])
                live_by_week[week] = {
                    p["id"]: p.get("stats", {}).get("total_points")
                    for p in (payload or {}).get("elements", [])
                }
            scores = live_by_week[week]
            base, selected = base_lines[week], selected_lines[week]
            required = set(base["xi"] + selected["xi"])
            if any(scores.get(pid) is None for pid in required):
                continue
            hold_points = (
                sum(scores[pid] for pid in base["xi"]) + scores[base["captain"]]
            )
            chosen_points = (
                sum(scores[pid] for pid in selected["xi"]) + scores[selected["captain"]]
            )
            weeks[str(week)] = {"hold": hold_points, "plan": chosen_points}
        if not weeks:
            continue
        gross = sum(value["plan"] - value["hold"] for value in weeks.values())
        complete = len(weeks) == len(report["weeks"])
        net = gross - chosen["hits"] if chosen["hits"] is not None else None
        record.outcomes = {
            "weeks": weeks,
            "completed": len(weeks),
            "complete": complete,
            "gross_gain": gross,
            "net_gain": net,
            "forecast_error": (
                round(net - chosen["net_gain"], 2)
                if complete and net is not None and chosen["net_gain"] is not None
                else None
            ),
        }
        record.save(update_fields=["outcomes"])
        reviewed += 1
    return reviewed


def calibration(records):
    completed = [
        record
        for record in records
        if record.snapshot.get("chosen", {}).get("moves")
        and record.outcomes.get("complete")
        and record.outcomes.get("forecast_error") is not None
    ]
    errors = [record.outcomes["forecast_error"] for record in completed]
    return {
        "completed_plans": len(completed),
        "mean_absolute_error": (
            round(sum(abs(error) for error in errors) / len(errors), 2)
            if errors
            else None
        ),
        "mean_error": round(sum(errors) / len(errors), 2) if errors else None,
    }
