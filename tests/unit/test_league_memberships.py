from io import StringIO
from unittest.mock import patch

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.db.backends.sqlite3.base import DatabaseWrapper
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from rivals.models import MiniLeague, MiniLeagueTeam, SyncState, Team, UserMiniLeague
from rivals.services.league_membership_service import reconcile_memberships
from rivals.services.refresh_service import enqueue_refresh
from rivals.tasks import ingest


@pytest.fixture
def owner(db):
    team = Team.objects.create(fpl_team_id="123", team_name="Current XI")
    return get_user_model().objects.create_user(
        username="owner", email="owner@example.com", team=team
    )


def membership(user, fpl_id, source="fpl"):
    league = MiniLeague.objects.create(
        fpl_league_id=str(fpl_id), name=f"League {fpl_id}"
    )
    return UserMiniLeague.objects.create(user=user, mini_league=league, source=source)


def profile(rows=None):
    return {
        "id": 123,
        "leagues": {
            "classic": (
                rows
                if rows is not None
                else [
                    {
                        "id": 10,
                        "name": "Current league",
                        "league_type": "x",
                        "scoring": "c",
                    },
                    {"id": 314, "name": "Overall", "league_type": "s"},
                ]
            )
        },
    }


def test_refresh_retires_only_missing_fpl_memberships_and_keeps_history(owner):
    old = membership(owner, 9)
    manual = membership(owner, 20, "manual")
    legacy = membership(owner, 30, "legacy")
    current = membership(owner, 10, "legacy")
    MiniLeagueTeam.objects.create(
        mini_league=old.mini_league,
        team=owner.team,
        season="2025/26",
        current_rank=1,
        total_points=500,
    )
    reconcile_memberships(owner, profile())
    for row in (old, manual, legacy, current):
        row.refresh_from_db()
    assert not old.active
    assert manual.active and manual.source == "manual"
    assert legacy.active and legacy.source == "legacy"
    assert current.active and current.source == "fpl"
    assert old.mini_league.mini_league_teams.get().total_points == 500
    assert not MiniLeague.objects.filter(fpl_league_id="314").exists()
    # A future snapshot can legitimately have no custom leagues.
    reconcile_memberships(owner, profile([]))
    current.refresh_from_db()
    assert not current.active


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        {"id": 123, "leagues": {}},
        {"id": 123, "leagues": {"classic": [None]}},
        {"id": 999, "leagues": {"classic": []}},
    ],
)
def test_invalid_or_wrong_account_profile_never_retires_memberships(owner, bad):
    old = membership(owner, 9)
    with pytest.raises(ValueError):
        reconcile_memberships(owner, bad, retire_legacy=True)
    old.refresh_from_db()
    assert old.active


def test_legacy_cleanup_is_dry_run_then_apply_and_preserves_manual_rows(owner):
    old = membership(owner, 9, "legacy")
    manual = membership(owner, 20, "manual")
    output = StringIO()
    with patch(
        "rivals.services.fpl_api_service.FplApiService.fetch_team_basic_info",
        return_value=profile(),
    ):
        call_command(
            "reconcile_fpl_leagues", user_id=owner.pk, retire_legacy=True, stdout=output
        )
        assert "Deactivate: 9" in output.getvalue()
        assert "Deactivate: 20" not in output.getvalue()
        old.refresh_from_db()
        assert old.active
        assert not MiniLeague.objects.filter(fpl_league_id="10").exists()
        call_command(
            "reconcile_fpl_leagues",
            user_id=owner.pk,
            retire_legacy=True,
            apply=True,
            stdout=StringIO(),
        )
    old.refresh_from_db()
    manual.refresh_from_db()
    assert not old.active and manual.active


def test_profile_worker_reconciles_memberships(owner):
    old = membership(owner, 9)
    state = SyncState.objects.create(
        key=f"profile:{owner.team_id}", kind="profile", object_id=owner.team_id
    )
    with (
        patch("rivals.tasks.FplApiService") as api,
        patch(
            "rivals.tasks.GameweekContextService.cached",
            return_value={"stale": False, "season": "2026/27"},
        ),
        patch(
            "rivals.services.team_sync_service.TeamSyncService.sync_basic_info",
            return_value=True,
        ),
        patch(
            "rivals.services.team_sync_service.TeamSyncService.detect_and_handle_season_transition"
        ),
    ):
        api.return_value.fetch_team_basic_info.return_value = profile()
        ingest(state)
    old.refresh_from_db()
    assert not old.active
    assert owner.customminileagues.get(mini_league__fpl_league_id="10").source == "fpl"


@pytest.mark.django_db
@pytest.mark.parametrize("busy", [True, False])
def test_fresh_or_busy_refresh_does_not_acquire_a_write_transaction(busy):
    now = timezone.now()
    context = {"season": "2026/27", "scoring_gameweek": 10}
    SyncState.objects.create(
        key="players",
        kind="players",
        status="queued" if busy else "ok",
        lease_until=now + timezone.timedelta(minutes=5) if busy else None,
        last_success_at=now,
        payload={"stamp": "2026/27:10"},
    )
    with CaptureQueriesContext(connection) as queries:
        assert not enqueue_refresh("players", context=context)
    assert queries and all(
        q["sql"].lstrip().upper().startswith("SELECT") for q in queries
    )


@pytest.mark.django_db
def test_wal_allows_readers_during_atomic_publication(tmp_path):
    config = {**settings.DATABASES["default"], "NAME": str(tmp_path / "shared.sqlite3")}
    writer = DatabaseWrapper(config, alias="wal_writer")
    reader = DatabaseWrapper(config, alias="wal_reader")
    try:
        with writer.cursor() as cursor:
            assert cursor.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            cursor.execute("CREATE TABLE standings (points INTEGER)")
            cursor.execute("INSERT INTO standings VALUES (100)")
        reader.ensure_connection()
        with writer.cursor() as cursor:
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute("UPDATE standings SET points=200")
        with reader.cursor() as cursor:
            assert cursor.execute("SELECT points FROM standings").fetchone()[0] == 100
        writer.commit()
        with reader.cursor() as cursor:
            assert cursor.execute("SELECT points FROM standings").fetchone()[0] == 200
    finally:
        writer.close()
        reader.close()
