import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connections
from django.test import RequestFactory
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from api.throttles import RequestThrottle
from core.models import RequestLimit
from core.rate_limits import reserve_request
from rivals.models import SyncState


@pytest.mark.django_db
def test_function_throttle_metadata_and_429():
    @api_view(["POST"])
    @permission_classes([AllowAny])
    @throttle_classes([RequestThrottle])
    def endpoint(request):
        return Response({"ok": True})

    endpoint.cls.post.rate_limit_action = "signup"
    factory = APIRequestFactory()
    with patch.dict("core.rate_limits.POLICIES", signup=(("ip", 1, 60),)):
        assert endpoint(factory.post("/")).status_code == 200
        assert endpoint(factory.post("/")).status_code == 429


@pytest.mark.django_db
def test_user_only_throttle_does_not_parse_malformed_json():
    from api.views import TeamSyncView
    from rest_framework.request import Request
    from rest_framework.parsers import JSONParser

    request = Request(
        APIRequestFactory().post("/", "{bad", content_type="application/json"),
        parsers=[JSONParser()],
    )
    request.user = SimpleNamespace(pk=123, is_authenticated=True)
    assert RequestThrottle().allow_request(request, TeamSyncView())
    assert RequestLimit.objects.get().attempts == 1


@pytest.mark.django_db
def test_rejected_ip_does_not_create_rotating_identity_buckets():
    request = RequestFactory().post("/", REMOTE_ADDR="203.0.113.1")
    request.user = AnonymousUser()
    with patch.dict(
        "core.rate_limits.POLICIES", login=(("ip", 1, 60), ("identity", 5, 60))
    ):
        assert reserve_request(request, "login", identity="first@example.com") == 0
        assert reserve_request(request, "login", identity="second@example.com") > 0
    assert RequestLimit.objects.count() == 2


@pytest.mark.django_db(transaction=True)
def test_two_simultaneous_reservations_on_existing_sqlite_bucket(tmp_path):
    # File-backed SQLite matches production (shared in-memory cache has different
    # SQLITE_LOCKED behavior and does not use SQLite's busy timeout).
    database = connections["default"].copy(alias="default")
    database.settings_dict = {
        **database.settings_dict,
        "NAME": str(tmp_path / "limits.sqlite3"),
    }
    with database.schema_editor() as editor:
        editor.create_model(RequestLimit)
    original = connections["default"]
    connections["default"] = database
    request = SimpleNamespace(
        META={}, user=SimpleNamespace(pk=5, is_authenticated=True)
    )
    with patch.dict("core.rate_limits.POLICIES", refresh=(("user", 2, 60),)):
        assert reserve_request(request, "refresh") == 0
        barrier = Barrier(2)

        def reserve():
            local = database.copy(alias="default")
            connections["default"] = local
            try:
                local.ensure_connection()
                barrier.wait(timeout=10)
                return reserve_request(request, "refresh")
            finally:
                local.close()

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: reserve(), range(2)))
            assert sorted(result > 0 for result in results) == [False, True]
            assert RequestLimit.objects.get().attempts == 2
        finally:
            database.close()
            connections["default"] = original


def test_trusted_proxy_preserves_distinct_clients_and_ignores_untrusted_headers():
    async def client(peer, forwarded):
        observed = []

        async def app(scope, receive, send):
            observed.append(scope["client"][0])

        middleware = ProxyHeadersMiddleware(app, trusted_hosts=["172.18.0.2"])
        await middleware(
            {
                "type": "http",
                "client": (peer, 80),
                "headers": [(b"x-forwarded-for", forwarded.encode())],
            },
            None,
            None,
        )
        return observed[0]

    assert asyncio.run(client("172.18.0.2", "198.51.100.1")) == "198.51.100.1"
    assert asyncio.run(client("172.18.0.2", "198.51.100.2")) == "198.51.100.2"
    assert asyncio.run(client("172.18.0.3", "198.51.100.1")) == "172.18.0.3"


@pytest.mark.django_db
def test_deploy_worker_check_rejects_previous_container_and_requires_new_tick(
    monkeypatch,
):
    import socket

    state = SyncState.objects.create(
        key="worker",
        kind="worker",
        last_success_at=timezone.now(),
        payload={"worker_id": "old-container"},
    )
    with pytest.raises(CommandError, match="new task-processing"):
        call_command("check_fpl_worker", local_progress=True)
    state.payload = {"worker_id": socket.gethostname()}
    state.save()
    with pytest.raises(CommandError, match="new task-processing"):
        call_command("check_fpl_worker", local_progress=True)

    def tick(seconds):
        state.last_success_at = timezone.now()
        state.save()

    monkeypatch.setattr("rivals.management.commands.check_fpl_worker.time.sleep", tick)
    call_command("check_fpl_worker", local_progress=True, wait=5)


def test_negative_landing_cache_avoids_recomputation():
    from rivals.services.landing_data_service import LandingDataService

    cache.clear()
    cache.set("landing_best_xi_expected_v2", False, 60)
    with patch("rivals.services.landing_data_service.FplApiService") as api:
        assert LandingDataService().get_best_xi() is None
        api.return_value.fetch_bootstrap_static.assert_not_called()
    cache.clear()


def test_kamal_gate_resolves_clear_values_role_overrides_and_secret_aliases(tmp_path):
    import os
    from pathlib import Path
    import shutil
    import subprocess

    ruby = shutil.which("ruby")
    if (
        not ruby
        or subprocess.run(
            [ruby, "-e", 'gem "kamal", "2.12.0"'], capture_output=True
        ).returncode
    ):
        pytest.skip("Kamal 2.12.0 Ruby gem is required")
    (tmp_path / "config").mkdir()
    (tmp_path / ".kamal").mkdir()
    (tmp_path / ".kamal/secrets").write_text(
        "COMMON_SECRET=disposable-common\nJOB_SECRET=disposable-job\nREGISTRY_PASSWORD=disposable-registry\n"
    )
    (tmp_path / "config/deploy.yml").write_text("""service: gate-test
image: example/gate-test
builder:
  arch: amd64
servers:
  web:
    - 192.0.2.1
  job:
    hosts:
      - 192.0.2.1
    env:
      clear:
        ROLE_NAME: job
      secret:
        - SECRET_KEY:JOB_SECRET
registry:
  username: example
  password:
    - REGISTRY_PASSWORD
env:
  clear:
    DEBUG: false
    ROLE_NAME: web
  secret:
    - SECRET_KEY:COMMON_SECRET
""")
    fake_uv = tmp_path / "uv"
    fake_uv.write_text("""#!/bin/sh
[ "$DEBUG" = false ] || exit 12
case "$ROLE_NAME:$SECRET_KEY" in
 web:disposable-common|job:disposable-job) exit 0;;
 *) exit 13;;
esac
""")
    fake_uv.chmod(0o755)
    helper = Path(__file__).resolve().parents[2] / ".kamal/check-production.rb"
    result = subprocess.run(
        [ruby, str(helper)],
        cwd=tmp_path,
        env={
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "HOME": os.environ["HOME"],
            "DEBUG": "true",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "disposable" not in result.stdout + result.stderr


def test_chip_advice_checks_fit_xi_candidates_and_qualifying_recovery_weeks():
    from rivals.services.chip_advice_service import chip_advice

    weeks = [11, 12, 13, 14]
    engine = SimpleNamespace(
        ids=list(range(1, 16)),
        players={pid: {"status": "d" if pid == 1 else "a"} for pid in range(1, 16)},
        risk={pid: 0 for pid in range(1, 16)},
        projections={pid: {week: 2 for week in weeks} for pid in range(1, 16)},
        name=lambda pid: f"Player {pid}",
        player_fixtures={
            pid: [{"gameweek": week, "opponents": ["FC"]} for week in weeks]
            for pid in range(1, 16)
        },
    )
    engine.projections[1] = {week: 13 for week in weeks}
    engine.projections[2].update({11: 10, 12: 12})
    for pid in range(3, 8):
        engine.projections[pid][11] = 0
    for pid in range(3, 7):
        engine.projections[pid][12] = 0
    baseline = {
        "lineups": [
            {"gameweek": week, "xi": list(range(1, 12)), "captain": 1} for week in weeks
        ]
    }
    advice = chip_advice(engine, baseline)
    assert advice[0]["gameweek"] == 12
    assert "Player 2" in advice[0]["detail"]
    assert "extra 11.0 over baseline captaincy" in advice[0]["detail"]
    assert advice[2]["gameweek"] == 12
    assert advice[2]["status"] == "Plan ahead"
