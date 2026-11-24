"""Bounded worker-only ingestion of recent player match histories."""

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from rivals.models import FplSnapshot, GameweekForecast, SyncState
from .forecast_evaluation import top_projected
from .gameweek_context_service import GameweekContextService
from .historical_forecast_features import usable_history


def read_histories(season):
    now = timezone.now()
    rows = FplSnapshot.objects.filter(
        key__startswith=f"element:{season}:", expires_at__gt=now, updated_at__lte=now
    )
    return {
        int(row.key.rsplit(":", 1)[1]): [
            h
            for h in row.payload.get("history", [])
            if (kickoff := parse_datetime(h.get("kickoff_time") or ""))
            and kickoff < now
        ]
        for row in rows
    }


def current_candidate_record(context):
    from .gameweek_forecast_service import EXPERIMENT_VERSION

    records = GameweekForecast.objects.filter(
        season=context["season"],
        gameweek=context.get("decision_gameweek"),
        captured_at__lte=timezone.now(),
        deadline__gt=timezone.now(),
    ).order_by("-captured_at", "-pk")
    return records.filter(version=EXPERIMENT_VERSION).first() or records.first()


def record_candidates(record, *, primary_only=False):
    if record is None:
        return set()
    ids = set()
    policy = record.inputs.get("promotion_policy", {})
    primary = {"heuristic-v1", policy.get("challenger", "decision-v11")}
    for name, values in record.predictions.items():
        if primary_only and name not in primary:
            continue
        if name == "heuristic-v1" or name.startswith("decision-v"):
            ids |= top_projected(
                {int(pid): value for pid, value in values.items()},
                policy.get("primary_top_k", 40),
            )
    return ids


def priority_candidates(context):
    """Use frozen candidate rankings for this deadline, never actual outcomes."""
    return record_candidates(current_candidate_record(context))


def history_readiness(context):
    record = current_candidate_record(context)
    candidates = record_candidates(record, primary_only=True)
    priority = record_candidates(record)
    threshold = (
        record.inputs.get("promotion_policy", {}).get(
            "maximum_common_candidate_fallback_share", 0.1
        )
        if record
        else 0.1
    )
    usable = {
        pid
        for pid, rows in read_histories(context["season"]).items()
        if usable_history(rows)
    }
    missing = candidates - usable
    retries = SyncState.objects.filter(
        key__in=[f"element-retry:{context['season']}:{pid}" for pid in missing],
        next_attempt_at__gt=timezone.now(),
    ).count()
    share = len(missing) / len(candidates) if candidates else None
    return {
        "season": context["season"],
        "gameweek": context.get("decision_gameweek"),
        "experiment": record.version if record else None,
        "captured_at": record.captured_at.isoformat() if record else None,
        "candidate_players": len(candidates),
        "usable_candidate_histories": len(candidates - missing),
        "fallback_share_now": share,
        "missing_candidate_ids": sorted(missing),
        "candidates_in_retry_backoff": retries,
        "maximum_fallback_share": threshold,
        "coverage_ready": share is not None and share <= threshold,
        "priority_players": len(priority),
        "priority_missing_candidate_ids": sorted(priority - usable),
        "capture_coverage": (
            record.inputs.get("common_candidate_history_coverage") if record else None
        ),
        "note": "Fresh usable history coverage only; not a model promotion verdict.",
    }


def refresh_histories(api, context, *, publication_guard=None):
    bootstrap = api.fetch_bootstrap_static()
    if not bootstrap:
        raise RuntimeError("Player history source unavailable")
    season = context["season"]
    if (
        not season
        or GameweekContextService.derive(bootstrap.get("events", []))["season"]
        != season
    ):
        raise RuntimeError("Player history source season changed")
    retry_prefix = f"element-retry:{season}:"
    retries = {
        row.key: row for row in SyncState.objects.filter(key__startswith=retry_prefix)
    }
    now = timezone.now()
    timestamps = dict(
        FplSnapshot.objects.filter(key__startswith=f"element:{season}:").values_list(
            "key", "updated_at"
        )
    )
    players = [
        p
        for p in bootstrap.get("elements", [])
        if not p.get("removed", False)
        and (
            not (retry := retries.get(f"{retry_prefix}{p['id']}"))
            or not retry.next_attempt_at
            or retry.next_attempt_at <= now
        )
    ]
    oldest = timezone.now() - timezone.timedelta(hours=3)
    players.sort(
        key=lambda p: timestamps.get(
            f"element:{season}:{p['id']}", oldest - timezone.timedelta(days=1)
        )
    )
    # Prefer the candidates being compared, while reserving five requests for
    # the rest of the pool so the full archive continues to advance.
    due = [
        p
        for p in players
        if timestamps.get(
            f"element:{season}:{p['id']}", oldest - timezone.timedelta(seconds=1)
        )
        < oldest
    ]
    priority = priority_candidates(context)
    relevant = [p for p in due if p["id"] in priority]
    remaining = [p for p in due if p["id"] not in priority]
    selected = relevant[:15] + remaining[:5]
    selected_ids = {p["id"] for p in selected}
    selected.extend(p for p in due if p["id"] not in selected_ids)
    result = {}
    for player in selected[:20]:
        key = f"element:{season}:{player['id']}"
        if timestamps.get(key, oldest - timezone.timedelta(seconds=1)) >= oldest:
            continue
        data = api.make_request(f"{api.BASE_URL}/element-summary/{player['id']}/")
        retry_key = f"{retry_prefix}{player['id']}"
        with transaction.atomic():
            if publication_guard:
                publication_guard()
            if data is not None and isinstance(data.get("history"), list):
                result[key] = {"history": data["history"]}
                SyncState.objects.filter(key=retry_key).delete()
            else:
                previous = retries.get(retry_key)
                failures = (previous.failures if previous else 0) + 1
                SyncState.objects.update_or_create(
                    key=retry_key,
                    defaults={
                        "kind": "forecast_history",
                        "object_id": player["id"],
                        "status": "error",
                        "failures": failures,
                        "last_attempt_at": timezone.now(),
                        "next_attempt_at": timezone.now()
                        + timezone.timedelta(
                            seconds=min(3600, 300 * 2 ** min(failures - 1, 6))
                        ),
                        "error": "Player history unavailable; retry scheduled.",
                    },
                )
    return result
