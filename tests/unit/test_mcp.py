"""Exercise the public read, OAuth and MCP interfaces with published fixtures."""

import asyncio
import base64
import hashlib
import json
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from oauth2_provider.models import AccessToken, Application, Grant, RefreshToken
from starlette.testclient import TestClient

from rivals.models import (
    FplSnapshot,
    GameweekData,
    LeagueStandingsPage,
    MiniLeague,
    MiniLeagueTeam,
    SquadHistory,
    SyncState,
    Team,
    TrackedTeam,
    Transfer,
    UserMiniLeague,
)
from rivals.services.published_reads import PublishedReads, ReadUnavailable
from rivals_mcp.models import Connection
from rivals_mcp.oauth import RivalsOAuthValidator, resource_url
from rivals_mcp.server import (
    DEFINITIONS,
    SCHEMAS,
    authenticate,
    call_tool,
    create_app,
    principal,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def published():
    now = timezone.now()
    events = [
        {"id": n, "deadline_time": (now + timedelta(days=(n - 3) * 7 + 1)).isoformat()}
        for n in (1, 2, 3, 4, 5, 6)
    ]
    year = (now - timedelta(days=13)).year
    season = f"{year}/{(year + 1) % 100:02d}"
    state = SyncState.objects.create(
        key="context",
        kind="context",
        status="ok",
        last_success_at=now,
        payload={"events": events},
    )
    own = Team.objects.create(
        fpl_team_id="100",
        team_name="Own FC",
        current_season=season,
        current_total_points=200,
        last_synced_at=now,
    )
    rival = Team.objects.create(
        fpl_team_id="200", team_name="Rival FC", current_season=season
    )
    foreign = Team.objects.create(
        fpl_team_id="300", team_name="Other FC", current_season=season
    )
    user = get_user_model().objects.create_user(
        username="mcp", email="mcp@example.com", is_verified=True, team=own
    )
    other = get_user_model().objects.create_user(
        username="other", email="other@example.com", is_verified=True, team=foreign
    )
    TrackedTeam.objects.create(user=user, team=rival)
    league = MiniLeague.objects.create(
        fpl_league_id="500", name="Friends", last_synced_at=now
    )
    UserMiniLeague.objects.create(user=user, mini_league=league)
    for team, points, rank in [(own, 200, 2), (rival, 210, 1)]:
        MiniLeagueTeam.objects.create(
            mini_league=league,
            team=team,
            total_points=points,
            current_rank=rank,
            season=season,
        )
        for week in (1, 2):
            GameweekData.objects.create(
                team=team, season=season, gameweek=week, points=40
            )
            for n in range(1, 16):
                SquadHistory.objects.create(
                    team=team,
                    season=season,
                    gameweek=week,
                    player_fpl_id=n,
                    position=n,
                    element_type=1,
                    player_name=f"Player {n}",
                    player_cost=50,
                    multiplier=2 if n == 1 else 1 if n <= 11 else 0,
                    is_captain=n == 1,
                    gameweek_points=3,
                    points_final=True,
                )
    bootstrap = {
        "events": events,
        "elements": [
            {"id": n, "team": 1, "web_name": f"Player {n}"} for n in range(1, 16)
        ],
        "teams": [{"id": 1, "name": "Arsenal"}, {"id": 2, "name": "Liverpool"}],
    }
    fixtures = [
        {
            "id": n,
            "event": w,
            "team_h": 1,
            "team_a": 2,
            "team_h_difficulty": 4,
            "team_a_difficulty": 3,
            "finished": False,
            "kickoff_time": (now + timedelta(days=10)).isoformat(),
        }
        for n, w in [(1, 3), (2, 3), (3, 5), (4, None)]
    ]
    for key, payload in [("bootstrap", bootstrap), ("fixtures", fixtures)]:
        FplSnapshot.objects.create(
            key=key,
            payload=payload,
            updated_at=now,
            expires_at=now + timedelta(minutes=15),
        )
    return locals()


def test_all_ten_reads_have_no_domain_or_job_writes(published):
    reads = PublishedReads(published["user"])
    arguments = {
        "get_league_standings": {"league": "league:500"},
        "compare_teams": {"second": "entry:200"},
        "get_upcoming_fixtures": {"team": "me"},
    }
    with (
        patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("No network"),
        ),
        CaptureQueriesContext(connection) as queries,
    ):
        results = {
            name: getattr(reads, name)(**arguments.get(name, {}))
            for name in DEFINITIONS
        }
    assert len(results) == 10
    assert all(
        not q["sql"]
        .lstrip()
        .upper()
        .startswith(("INSERT", "UPDATE", "DELETE", "REPLACE"))
        for q in queries
    )
    assert results["get_team_squad"]["gameweek"] == 2
    assert len(results["compare_teams"]["data"]["shared_players"]) == 15
    weeks = results["get_upcoming_fixtures"]["data"][0]["gameweeks"]
    assert [len(w["fixtures"]) for w in weeks] == [2, 0, 1]
    assert len(results["get_upcoming_fixtures"]["data"][0]["unscheduled"]) == 1


def test_missing_expired_reads_do_not_enqueue(published):
    FplSnapshot.objects.update(expires_at=timezone.now() - timedelta(days=1))
    SquadHistory.objects.all().delete()
    reads = PublishedReads(published["user"])
    with patch(
        "rivals.services.refresh_service.enqueue_refresh",
        side_effect=AssertionError("No scheduling"),
    ):
        assert reads.get_team_squad()["coverage"] == "unavailable"
        assert (
            reads.get_upcoming_fixtures(clubs=["club:1"])["sources"][0]["status"]
            == "stale"
        )
        FplSnapshot.objects.all().delete()
        assert (
            reads.get_upcoming_fixtures(clubs=["club:1"])["coverage"] == "unavailable"
        )


def test_access_rechecked_and_private_data_excluded(published):
    reads = PublishedReads(published["other"])
    for name, args in [
        ("get_team_summary", {"team": "entry:100"}),
        ("get_league_standings", {"league": "league:500"}),
    ]:
        with pytest.raises(ReadUnavailable):
            getattr(reads, name)(**args)
    user_reads = PublishedReads(published["user"])
    assert user_reads.team("entry:200") == published["rival"]
    TrackedTeam.objects.filter(user=published["user"]).delete()
    UserMiniLeague.objects.filter(user=published["user"]).update(active=False)
    with pytest.raises(ReadUnavailable):
        user_reads.team("entry:200")
    encoded = json.dumps(user_reads.get_my_context(), default=str)
    assert published["user"].email not in encoded
    assert "password" not in encoded


def test_cursors_are_bound_to_account_filters_and_publication(published):
    reads = PublishedReads(published["user"])
    result = reads.get_team_history(limit=1)
    cursor = result["next_cursor"]
    assert cursor
    assert (
        reads.get_team_history(limit=1, cursor=cursor)["data"]["rows"][0]["gameweek"]
        == 1
    )
    with pytest.raises(ReadUnavailable):
        reads.get_team_history(limit=1, cursor=cursor, start=2)
    GameweekData.objects.filter(team=published["own"]).update(updated_at=timezone.now())
    with pytest.raises(ReadUnavailable):
        reads.get_team_history(limit=1, cursor=cursor)
    with pytest.raises(ReadUnavailable):
        PublishedReads(published["other"]).get_team_history(limit=1, cursor=cursor)


def test_partial_summary_league_does_not_create_pages(published):
    league = published["league"]
    league.summary_only = True
    league.save()
    reads = PublishedReads(published["user"])
    assert reads.get_league_standings("league:500")["coverage"] == "unavailable"
    assert LeagueStandingsPage.objects.count() == 0
    LeagueStandingsPage.objects.create(
        mini_league=league,
        season=published["season"],
        number=1,
        updated_at=timezone.now(),
        rows=[{"entry": 200, "rank": 1, "total": 210}],
        has_next=True,
    )
    assert reads.get_league_standings("league:500")["coverage"] == "partial"
    assert LeagueStandingsPage.objects.count() == 1


def test_summary_entry_access_uses_current_publication(published):
    TrackedTeam.objects.all().delete()
    MiniLeagueTeam.objects.all().delete()
    now = timezone.now()
    LeagueStandingsPage.objects.create(
        mini_league=published["league"],
        season=published["season"],
        number=1,
        updated_at=now,
        rows=[{"entry": 200, "rank": 1, "total": 210}],
    )
    older = LeagueStandingsPage.objects.create(
        mini_league=published["league"],
        season=published["season"],
        number=2,
        updated_at=now - timedelta(hours=1),
        rows=[{"entry": 300, "rank": 60, "total": 190}],
    )
    reads = PublishedReads(published["user"])
    assert reads.team("entry:200") == published["rival"]
    with pytest.raises(ReadUnavailable):
        reads.team("entry:300")
    older.updated_at = now
    older.save()
    assert reads.team("entry:300") == published["foreign"]


def test_comparison_does_not_mix_gameweeks(published):
    SquadHistory.objects.filter(team=published["own"], gameweek=1).delete()
    SquadHistory.objects.filter(team=published["rival"], gameweek=2).delete()
    assert (
        PublishedReads(published["user"]).compare_teams(second="entry:200")["coverage"]
        == "unavailable"
    )


def test_transfer_prices_are_millions_and_individual_hits_unknown(published):
    Transfer.objects.create(
        team=published["own"],
        season=published["season"],
        gameweek=2,
        player_in_name="Incoming",
        player_out_name="Outgoing",
        player_in_cost=75,
        player_out_cost=None,
    )
    row = PublishedReads(published["user"]).get_team_transfers()["data"]["rows"][0]
    assert float(row["buy_price"]["value"]) == 7.5
    assert row["sell_price"]["value"] is None
    assert row["point_cost"] is None


def test_summary_standings_cursors_preserve_all_published_rows(published):
    league = published["league"]
    league.summary_only = True
    league.save()
    LeagueStandingsPage.objects.create(
        mini_league=league,
        season=published["season"],
        number=1,
        updated_at=timezone.now(),
        rows=[{"entry": n, "rank": n, "total": 200 - n} for n in range(1, 51)],
    )
    reads = PublishedReads(published["user"])
    first = reads.get_league_standings("league:500", limit=20)
    second = reads.get_league_standings(
        "league:500", cursor=first["next_cursor"], limit=20
    )
    third = reads.get_league_standings(
        "league:500", cursor=second["next_cursor"], limit=20
    )
    references = [
        r["reference"] for page in (first, second, third) for r in page["data"]["rows"]
    ]
    assert references == [f"entry:{n}" for n in range(1, 51)]
    assert third["next_cursor"] is None


def test_unknown_season_does_not_expose_unclassified_league_entries(published):
    SyncState.objects.filter(key="context").delete()
    TrackedTeam.objects.all().delete()
    MiniLeagueTeam.objects.update(season=None)
    reads = PublishedReads(published["user"])
    assert reads.get_league_standings("league:500")["coverage"] == "unavailable"
    with pytest.raises(ReadUnavailable):
        reads.get_team_summary("entry:200")


def oauth_client(client):
    response = client.post(
        reverse("oauth2_provider:dcr-register"),
        json.dumps(
            {
                "client_name": "Test assistant",
                "redirect_uris": ["https://assistant.example/callback"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            }
        ),
        content_type="application/json",
    )
    assert response.status_code == 201, response.content
    return response.json()["client_id"]


def authorize(client, user, client_id, *, allow=True, **overrides):
    verifier = "v" * 64
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    params = {
        "client_id": client_id,
        "redirect_uri": "https://assistant.example/callback",
        "response_type": "code",
        "scope": "rivals:read",
        "state": "test-state",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": resource_url(),
    }
    params.update(overrides)
    client.force_login(user)
    consent = client.get(reverse("oauth2_provider:authorize"), params)
    if overrides:
        return consent
    assert consent.status_code == 200, consent.content
    assert b"Allow read access" in consent.content
    form = consent.context["form"]
    values = {
        name: field.value() or ""
        for name, field in ((n, form[n]) for n in form.fields)
        if field.is_hidden
    }
    if allow:
        values["allow"] = "true"
    response = client.post(reverse("oauth2_provider:authorize"), values)
    assert response.status_code == 302, response.content
    query = parse_qs(urlsplit(response["Location"]).query)
    return query, verifier


def issue_token(client, user):
    client_id = oauth_client(client)
    params, verifier = authorize(client, user, client_id)
    response = client.post(
        reverse("oauth2_provider:token"),
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": params["code"][0],
            "code_verifier": verifier,
            "redirect_uri": "https://assistant.example/callback",
            "resource": resource_url(),
        },
    )
    assert response.status_code == 200, response.content
    return response.json(), client_id


def test_oauth_connect_refresh_revoke_and_reconnect(client, published):
    tokens, client_id = issue_token(client, published["user"])
    raw = tokens["access_token"]
    assert authenticate(raw, "127.0.0.1")[1] == 200
    row = AccessToken.objects.get(
        token_checksum=hashlib.sha256(raw.encode()).hexdigest()
    )
    assert not row.token  # Hash at rest, not a raw credential.
    assert row.resource == [resource_url()]
    response = client.post(
        reverse("oauth2_provider:token"),
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": tokens["refresh_token"],
            "resource": resource_url(),
        },
    )
    assert response.status_code == 200, response.content
    assert response.json()["refresh_token"] != tokens["refresh_token"]
    grant = Connection.objects.get(user=published["user"])
    assert grant.last_used_at
    client.post(reverse("accounts:mcp"), {"connection": grant.pk})
    assert authenticate(response.json()["access_token"], "127.0.0.1")[1] == 401
    assert not AccessToken.objects.filter(user=published["user"]).exists()
    assert not RefreshToken.objects.filter(user=published["user"]).exists()
    assert not Grant.objects.filter(user=published["user"]).exists()
    failed = client.post(
        reverse("oauth2_provider:token"),
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": response.json()["refresh_token"],
            "resource": resource_url(),
        },
    )
    assert failed.status_code == 400
    params, _ = authorize(client, published["user"], client_id)
    assert "code" in params
    assert Connection.objects.get(pk=grant.pk).revoked_at is None


def test_oauth_rejects_unsafe_consent_and_unverified_accounts(client, published):
    client_id = oauth_client(client)
    for overrides in [
        {"resource": "https://attacker.example/mcp"},
        {"code_challenge_method": "plain"},
        {"scope": "write"},
        {"redirect_uri": "https://attacker.example/callback"},
    ]:
        response = authorize(client, published["user"], client_id, **overrides)
        assert response.status_code == 400
        assert "Location" not in response
    params, _ = authorize(client, published["user"], client_id, allow=False)
    assert params["error"] == ["access_denied"]
    assert not Connection.objects.exists()
    published["user"].is_verified = False
    published["user"].save()
    assert (
        authorize(
            client, published["user"], client_id, resource=resource_url()
        ).status_code
        == 302
    )


def test_wrong_audience_expiry_verification_and_disconnect_ownership(client, published):
    tokens, _ = issue_token(client, published["user"])
    raw = tokens["access_token"]
    row = AccessToken.objects.get(user=published["user"])
    row.resource = ["https://other.example"]
    row.save()
    assert authenticate(raw, "127.0.0.1")[1] == 401
    row.resource = [resource_url()]
    row.expires = timezone.now() - timedelta(seconds=1)
    row.save()
    assert authenticate(raw, "127.0.0.1")[1] == 401
    row.expires = timezone.now() + timedelta(hours=1)
    row.save()
    client.force_login(published["other"])
    client.post(
        reverse("accounts:mcp"),
        {"connection": Connection.objects.get(user=published["user"]).pk},
    )
    assert authenticate(raw, "127.0.0.1")[1] == 200
    published["user"].is_verified = False
    published["user"].save()
    assert authenticate(raw, "127.0.0.1")[1] == 401


def test_discovery_and_settings(client, published):
    metadata = client.get("/.well-known/oauth-authorization-server").json()
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert metadata["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert "registration_endpoint" in metadata
    resource = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert resource["resource"] == resource_url()
    assert client.get(reverse("accounts:mcp")).status_code == 302
    client.force_login(published["user"])
    page = client.get(reverse("accounts:mcp"))
    assert page.status_code == 200
    for phrase in [
        b"ChatGPT",
        b"Claude",
        b"Gemini",
        b"No assistants connected yet",
        b"Copy connection address",
    ]:
        assert phrase in page.content
    assert b'aria-current="page"' in page.content
    assert b"MCP" in client.get(reverse("accounts:gemini")).content


def test_pkce_verifier_and_code_are_single_use(client, published):
    client_id = oauth_client(client)
    params, verifier = authorize(client, published["user"], client_id)
    data = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": params["code"][0],
        "code_verifier": "incorrect" * 8,
        "redirect_uri": "https://assistant.example/callback",
        "resource": resource_url(),
    }
    assert client.post(reverse("oauth2_provider:token"), data).status_code == 400
    data["code_verifier"] = verifier
    assert client.post(reverse("oauth2_provider:token"), data).status_code == 200
    assert client.post(reverse("oauth2_provider:token"), data).status_code == 400


def test_refresh_replay_invalidates_family(client, published):
    tokens, client_id = issue_token(client, published["user"])
    data = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": tokens["refresh_token"],
        "resource": resource_url(),
    }
    rotated = client.post(reverse("oauth2_provider:token"), data)
    assert rotated.status_code == 200
    assert client.post(reverse("oauth2_provider:token"), data).status_code == 400
    assert authenticate(rotated.json()["access_token"], "127.0.0.1")[1] == 401


def test_wrong_resource_on_exchange_cannot_issue_token(client, published):
    client_id = oauth_client(client)
    params, verifier = authorize(client, published["user"], client_id)
    response = client.post(
        reverse("oauth2_provider:token"),
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": params["code"][0],
            "code_verifier": verifier,
            "redirect_uri": "https://assistant.example/callback",
            "resource": "https://attacker.example/mcp",
        },
    )
    assert response.status_code == 400
    assert not AccessToken.objects.filter(user=published["user"]).exists()


def test_connection_writes_require_csrf(client, published):
    issue_token(client, published["user"])
    protected = Client(enforce_csrf_checks=True)
    protected.force_login(published["user"])
    grant = Connection.objects.get(user=published["user"])
    assert (
        protected.post(reverse("accounts:mcp"), {"connection": grant.pk}).status_code
        == 403
    )
    assert (
        protected.post(
            reverse("oauth2_provider:authorize"), {"allow": "true"}
        ).status_code
        == 403
    )
    grant.refresh_from_db()
    assert grant.revoked_at is None


def test_registration_and_read_rate_limits(client, published):
    for _ in range(10):
        oauth_client(client)
    assert (
        client.post(
            reverse("oauth2_provider:dcr-register"),
            "{}",
            content_type="application/json",
        ).status_code
        == 429
    )
    from core.models import RequestLimit

    RequestLimit.objects.all().delete()
    tokens, _ = issue_token(client, published["user"])
    raw = tokens["access_token"]
    for _ in range(60):
        assert authenticate(raw, "127.0.0.1")[1] == 200
    assert authenticate(raw, "127.0.0.1")[1] == 429


def test_read_permission_required_and_settings_never_show_tokens(client, published):
    tokens, _ = issue_token(client, published["user"])
    page = client.get(reverse("accounts:mcp"))
    assert tokens["access_token"].encode() not in page.content
    assert tokens["refresh_token"].encode() not in page.content
    AccessToken.objects.filter(user=published["user"]).update(scope="other")
    assert authenticate(tokens["access_token"], "127.0.0.1")[1] == 403


def test_feature_switch_stops_new_connections(client, published):
    with override_settings(MCP_ENABLED=False):
        assert (
            client.post(
                reverse("oauth2_provider:dcr-register"),
                "{}",
                content_type="application/json",
            ).status_code
            == 503
        )
        assert client.get(reverse("oauth2_provider:authorize")).status_code == 503
        client.force_login(published["user"])
        assert b"temporarily unavailable" in client.get(reverse("accounts:mcp")).content


@pytest.mark.django_db(transaction=True)
def test_actual_mcp_transport_and_tools(client, published):
    tokens, _ = issue_token(client, published["user"])
    with override_settings(
        ALLOWED_HOSTS=["testserver"], MCP_ALLOWED_ORIGINS=["https://trusted.example"]
    ):
        with TestClient(create_app()) as http:
            headers = {
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {tokens['access_token']}",
            }
            missing = http.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={"Accept": headers["Accept"]},
            )
            assert missing.status_code == 401
            assert "resource_metadata" in missing.headers["WWW-Authenticate"]
            forbidden = http.post(
                "/mcp", json={}, headers={**headers, "Origin": "https://evil.example"}
            )
            assert forbidden.status_code == 403
            response = http.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                },
                headers=headers,
            )
            assert response.status_code == 200, response.text
            tools = http.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                headers=headers,
            ).json()
            assert len(tools["result"]["tools"]) == 10
            for tool in tools["result"]["tools"]:
                assert tool["annotations"]["readOnlyHint"]
            result = http.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "get_team_squad", "arguments": {}},
                },
                headers=headers,
            ).json()
            assert "error" not in result, result
            assert not result["result"].get("isError"), result
            data = json.loads(result["result"]["content"][0]["text"])
            assert data["gameweek"] == 2
            assert len(data["data"]["players"]) == 15
            invalid = http.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "get_team_squad", "arguments": {"gameweek": 99}},
                },
                headers=headers,
            ).json()
            assert invalid["result"]["isError"]


@pytest.mark.parametrize("remaining_seconds", [600, -60])
def test_profile_summary_uses_one_publication_and_stored_expiry(
    published, remaining_seconds
):
    now = timezone.now()
    profile = FplSnapshot.objects.create(
        key=f"profile:{published['season']}:100",
        payload={"current_event": 2, "summary_event_points": 0},
        updated_at=now - timedelta(minutes=40),
        expires_at=now + timedelta(seconds=remaining_seconds),
    )
    # The linked Team object can lag a worker's newer profile publication.
    published["own"].current_event_points = 99
    published["own"].save(update_fields=["current_event_points"])
    summary = PublishedReads(published["user"]).get_team_summary()
    assert summary["gameweek"] == 2
    assert summary["data"]["gameweek_points"] == 0
    assert summary["sources"][0]["updated_at"] == profile.updated_at
    assert summary["sources"][0]["expires_at"] == profile.expires_at
    assert summary["sources"][0]["status"] == (
        "fresh" if remaining_seconds > 0 else "stale"
    )


def test_profile_summary_without_event_keeps_points_unknown(published):
    reads = PublishedReads(published["user"])
    fallback = reads.get_team_summary()
    assert fallback["gameweek"] is None
    assert fallback["data"]["gameweek_points"] is None
    assert fallback["sources"][0]["expires_at"] == published[
        "own"
    ].last_synced_at + timedelta(minutes=30)
    FplSnapshot.objects.create(
        key=f"profile:{published['season']}:100",
        payload={"summary_event_points": 42},
        updated_at=timezone.now(),
        expires_at=timezone.now() + timedelta(hours=1),
    )
    assert reads.get_team_summary()["data"]["gameweek_points"] is None


def test_cold_context_is_a_stored_read_without_refresh(published):
    SyncState.objects.filter(key="context").delete()
    with (
        patch(
            "requests.sessions.Session.request", side_effect=AssertionError("No HTTP")
        ),
        patch(
            "rivals.services.refresh_service.enqueue_refresh",
            side_effect=AssertionError("No ingestion"),
        ),
        CaptureQueriesContext(connection) as queries,
    ):
        reads = PublishedReads(published["user"])
        assert reads.context["stale"]
        assert reads.get_my_context()["coverage"] == "unavailable"
    assert all(q["sql"].lstrip().upper().startswith("SELECT") for q in queries)


@pytest.mark.parametrize(
    "name,arguments,integer_fields",
    [
        ("get_upcoming_fixtures", {"team": "me", "horizon": 3.0}, ["horizon"]),
        (
            "get_team_history",
            {"limit": 20.0, "start": 1.0, "end": 3.0, "season": None},
            ["limit", "start", "end"],
        ),
        ("get_team_squad", {"gameweek": 2.0, "team": "me"}, ["gameweek"]),
        ("compare_teams", {"second": "entry:200", "gameweek": None}, []),
    ],
)
def test_validated_integral_arguments_are_normalized_before_dispatch(
    name, arguments, integer_fields
):
    def read(tool_name, normalized, user_id):
        assert tool_name == name
        assert normalized == arguments
        for field in integer_fields:
            assert type(normalized[field]) is int
        for field in normalized.keys() - set(integer_fields):
            assert type(normalized[field]) is type(arguments[field])
        return "{}"

    saved = principal.set(1)
    try:
        with patch("rivals_mcp.server.run_read", side_effect=read) as dispatch:
            result = asyncio.run(call_tool(name, arguments))
        assert not result.isError
        dispatch.assert_called_once()
    finally:
        principal.reset(saved)


def test_fractional_integer_argument_still_fails_validation():
    saved = principal.set(1)
    try:
        with patch("rivals_mcp.server.run_read") as dispatch:
            result = asyncio.run(
                call_tool("get_upcoming_fixtures", {"team": "me", "horizon": 3.5})
            )
        assert result.isError
        dispatch.assert_not_called()
    finally:
        principal.reset(saved)


@pytest.mark.parametrize(
    "allowed,host,status",
    [
        (["rivals.webbe.dev"], "rivals.webbe.dev", 401),
        (["rivals.webbe.dev"], "rivals.webbe.dev:443", 401),
        (["rivals.webbe.dev"], "rivals.webbe.dev:8000", 401),
        (["rivals.webbe.dev"], "rivals.webbe.dev.attacker.example:443", 421),
        (["rivals.webbe.dev"], "attacker.rivals.webbe.dev", 421),
        (["localhost:*"], "localhost:8000", 401),
        (["localhost:8000"], "localhost:9000", 421),
        (["[::1]"], "[::1]:8000", 401),
    ],
)
def test_sdk_host_rules_preserve_explicit_hosts_and_accept_ports(allowed, host, status):
    with override_settings(ALLOWED_HOSTS=allowed, DEBUG=False):
        with TestClient(create_app()) as http:
            assert http.get("/mcp", headers={"Host": host}).status_code == status


@pytest.mark.parametrize("grant_type", ["refresh_token", "authorization_code"])
@pytest.mark.parametrize("reconnect", [False, True])
def test_disconnect_between_validation_and_save_rejects_old_exchange(
    client, published, grant_type, reconnect
):
    if grant_type == "refresh_token":
        tokens, client_id = issue_token(client, published["user"])
        data = {
            "grant_type": grant_type,
            "client_id": client_id,
            "refresh_token": tokens["refresh_token"],
            "resource": resource_url(),
        }
    else:
        client_id = oauth_client(client)
        params, verifier = authorize(client, published["user"], client_id)
        data = {
            "grant_type": grant_type,
            "client_id": client_id,
            "code": params["code"][0],
            "code_verifier": verifier,
            "redirect_uri": "https://assistant.example/callback",
            "resource": resource_url(),
        }

    save = RivalsOAuthValidator.save_bearer_token
    fresh = None

    def disconnect_before_transaction(validator, token, request, *args, **kwargs):
        nonlocal fresh
        # OAuthlib has already validated the old credential and cached its state.
        assert request.user.pk == published["user"].pk
        if grant_type == "refresh_token":
            assert request.refresh_token_instance.pk
        grant = Connection.objects.get(user=published["user"])
        client.post(reverse("accounts:mcp"), {"connection": grant.pk})
        assert not AccessToken.objects.filter(user=published["user"]).exists()
        if reconnect:
            fresh = authorize(client, published["user"], client_id)
        return save(validator, token, request, *args, **kwargs)

    with patch.object(
        RivalsOAuthValidator,
        "save_bearer_token",
        autospec=True,
        side_effect=disconnect_before_transaction,
    ):
        response = client.post(reverse("oauth2_provider:token"), data)
    assert response.status_code == 400, response.content
    assert response.json()["error"] == "invalid_grant"
    assert not AccessToken.objects.filter(user=published["user"]).exists()
    assert not RefreshToken.objects.filter(user=published["user"]).exists()
    if fresh:
        params, verifier = fresh
        response = client.post(
            reverse("oauth2_provider:token"),
            {
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": params["code"][0],
                "code_verifier": verifier,
                "redirect_uri": "https://assistant.example/callback",
                "resource": resource_url(),
            },
        )
        assert response.status_code == 200, response.content
        assert authenticate(response.json()["access_token"], "127.0.0.1")[1] == 200


def test_authentication_does_not_revive_cached_access_after_reconnect(
    client, published
):
    tokens, client_id = issue_token(client, published["user"])
    lock = Connection.objects.select_for_update
    disconnected = False

    def disconnect_after_token_lookup(*args, **kwargs):
        nonlocal disconnected
        if not disconnected:
            disconnected = True
            grant = Connection.objects.get(user=published["user"])
            client.post(reverse("accounts:mcp"), {"connection": grant.pk})
            authorize(client, published["user"], client_id)
        return lock(*args, **kwargs)

    with patch.object(
        Connection.objects,
        "select_for_update",
        side_effect=disconnect_after_token_lookup,
    ):
        assert authenticate(tokens["access_token"], "127.0.0.1")[1] == 401
    assert disconnected
    assert not AccessToken.objects.filter(user=published["user"]).exists()
