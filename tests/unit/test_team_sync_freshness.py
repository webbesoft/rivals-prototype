"""Team freshness follows successful background publications."""

from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from django.utils.timesince import timesince

from rivals.models import SyncState, Team
from rivals.services.fpl_snapshot_service import publish_snapshots
from rivals.services.refresh_service import enqueue_refresh, enqueue_team
from rivals.tasks import refresh_resource
from tests.unit.test_epoch3_squads import NOW, api, manager

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def isolated():
    cache.clear()
    with (
        patch("django.utils.timezone.now", return_value=NOW),
        patch(
            "django.template.defaultfilters.timesince",
            side_effect=lambda value, arg=None: timesince(value, arg or NOW),
        ),
        patch("requests.get", side_effect=AssertionError("Unexpected HTTP request")),
    ):
        yield
    cache.clear()


@pytest.mark.parametrize(
    "job_order",
    [
        ("profile", "history", "transfers", "squad"),
        ("squad", "transfers", "history", "profile"),
    ],
)
def test_manual_sync_updates_displayed_last_synced_after_worker_finishes(
    client, manager, api, job_order
):
    manager.fpl_id = manager.team.fpl_team_id
    manager.is_verified = True
    manager.save(update_fields=["fpl_id", "is_verified"])
    client.force_login(manager)
    old_sync = NOW - timezone.timedelta(days=16)
    Team.objects.filter(pk=manager.team_id).update(last_synced_at=old_sync)
    publish_snapshots(
        {"bootstrap": api.fetch_bootstrap_static.return_value, "fixtures": []}
    )

    response = client.post(reverse("rivals:teams.sync", args=[manager.team_id]))
    assert response.status_code == 302
    assert response.url == reverse("rivals:teams.show", args=[manager.team_id])
    manager.team.refresh_from_db()
    assert manager.team.last_synced_at == old_sync
    queued_page = client.get(response.url)
    assert queued_page.status_code == 200
    assert f"{timesince(old_sync, NOW)} ago</span>" in queued_page.content.decode()

    states = {
        state.kind: state
        for state in SyncState.objects.filter(kind__in=job_order, status="queued")
    }
    assert set(states) == set(job_order)
    completed = set()
    with patch("rivals.tasks.FplApiService", return_value=api):
        for kind in job_order:
            state = states[kind]
            refresh_resource.now(state.key, state.generation)
            state.refresh_from_db()
            assert state.status == "ok"
            assert state.last_success_at == NOW
            completed.add(kind)
            manager.team.refresh_from_db()
            assert manager.team.last_synced_at == (
                NOW if {"profile", "history", "transfers"} <= completed else old_sync
            )

    assert manager.team.current_total_points == 601
    response = client.get(reverse("rivals:teams.show", args=[manager.team_id]))
    assert response.status_code == 200
    html = response.content.decode()
    assert f"{timesince(old_sync, NOW)} ago</span>" not in html
    assert f"{timesince(NOW, NOW)} ago</span>" in html


@pytest.mark.parametrize("failed_kind", ["profile", "history", "transfers"])
def test_failed_team_resource_preserves_last_synced(manager, api, failed_kind):
    old_sync = NOW - timezone.timedelta(days=16)
    Team.objects.filter(pk=manager.team_id).update(last_synced_at=old_sync)
    fetch = {
        "profile": api.fetch_team_basic_info,
        "history": api.fetch_team_history,
        "transfers": api.fetch_team_transfers,
    }
    fetch[failed_kind].return_value = None
    enqueue_team(manager.team_id)

    with patch("rivals.tasks.FplApiService", return_value=api):
        for state in SyncState.objects.filter(
            kind__in=fetch, object_id=manager.team_id
        ):
            refresh_resource.now(state.key, state.generation)
            state.refresh_from_db()
            assert state.status == ("error" if state.kind == failed_kind else "ok")
            manager.team.refresh_from_db()
            assert manager.team.last_synced_at == old_sync


@pytest.mark.parametrize("stamp", ["2025/26:10", "2026/27:9"])
def test_old_season_or_gameweek_success_cannot_complete_team_sync(manager, api, stamp):
    for kind in ("history", "transfers"):
        SyncState.objects.create(
            key=f"{kind}:{manager.team_id}",
            kind=kind,
            object_id=manager.team_id,
            status="ok",
            last_success_at=NOW,
            payload={"stamp": stamp},
        )
    enqueue_refresh("profile", manager.team_id)
    state = SyncState.objects.get(key=f"profile:{manager.team_id}")

    with patch("rivals.tasks.FplApiService", return_value=api):
        refresh_resource.now(state.key, state.generation)

    state.refresh_from_db()
    assert state.status == "ok"
    manager.team.refresh_from_db()
    assert manager.team.last_synced_at is None


@pytest.mark.parametrize("previous_age_minutes", [None, 60, 1])
def test_team_sync_uses_oldest_resource_without_moving_backwards(
    manager, api, previous_age_minutes
):
    previous = (
        NOW - timezone.timedelta(minutes=previous_age_minutes)
        if previous_age_minutes is not None
        else None
    )
    Team.objects.filter(pk=manager.team_id).update(last_synced_at=previous)
    oldest = NOW - timezone.timedelta(minutes=5)
    for kind, synced_at in (
        ("history", oldest),
        ("transfers", NOW - timezone.timedelta(minutes=2)),
    ):
        SyncState.objects.create(
            key=f"{kind}:{manager.team_id}",
            kind=kind,
            object_id=manager.team_id,
            status="ok",
            last_success_at=synced_at,
            payload={"stamp": "2026/27:10"},
        )
    enqueue_refresh("profile", manager.team_id)
    state = SyncState.objects.get(key=f"profile:{manager.team_id}")

    with patch("rivals.tasks.FplApiService", return_value=api):
        refresh_resource.now(state.key, state.generation)

    state.refresh_from_db()
    assert state.status == "ok"
    manager.team.refresh_from_db()
    assert manager.team.last_synced_at == max(previous or oldest, oldest)
