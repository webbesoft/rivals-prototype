"""Bounded worker-only ingestion of recent player match histories."""

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from rivals.models import FplSnapshot, SyncState
from .gameweek_context_service import GameweekContextService


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
    result = {}
    for player in players[:20]:
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
