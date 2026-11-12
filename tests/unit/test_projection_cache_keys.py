from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
import warnings

import pytest
from django.core.cache import cache
from django.core.cache.backends.base import CacheKeyWarning

from rivals.services.projection_service import ProjectionService
from rivals.models import MiniLeague, MiniLeagueTeam, Team


@pytest.mark.parametrize(
    "synced_at", [None, datetime(2026, 11, 10, 16, 10, 27, 975855, tzinfo=timezone.utc)]
)
def test_league_projection_cache_is_portable_and_invalidates_after_sync(synced_at):
    team = Mock(
        id=1, team_name="My team", manager_name="Manager", current_total_points=500
    )
    standing = Mock(team=team, total_points=500, current_rank=1)
    league = Mock(id=1, last_synced_at=synced_at, uses_summary=False)
    league.name = "My league"
    league.mini_league_teams.select_related.return_value.all.return_value = [standing]
    service = ProjectionService()
    cache.clear()
    try:
        with patch(
            "rivals.services.projection_service.GameweekContextService.cached",
            return_value={"season": "2026/27", "scoring_gameweek": 10},
        ) as context_lookup:
            with warnings.catch_warnings():
                warnings.simplefilter("error", CacheKeyWarning)
                first = service.project_mini_league(league)
                assert service.project_mini_league(league) == first
                league.mini_league_teams.select_related.assert_called_once()
                standing.total_points = 600
                league.last_synced_at = (
                    synced_at or datetime(2026, 11, 10, tzinfo=timezone.utc)
                ) + timedelta(seconds=1)
                refreshed = service.project_mini_league(league)
                assert refreshed["standings"][0]["current_points"] == 600
                assert league.mini_league_teams.select_related.call_count == 2
                context_lookup.assert_called_once()
    finally:
        cache.clear()


@pytest.mark.django_db
@pytest.mark.parametrize("gameweek", [1, 2, 11, 38, 39])
@pytest.mark.parametrize("size", [1, 2, 6])
def test_small_summary_matches_full_projection(gameweek, size):
    league = MiniLeague.objects.create(fpl_league_id="summary-test", name="League")
    # Include tied points/ranks, zero ranks, and zero scores that fall back to
    # the team's stored total. Compare every manager, including tied entries.
    entries = [
        (500, 1, 500),
        (500, 1, 500),
        (0, 0, 300),
        (400, 3, 400),
        (0, 5, 0),
        (500, 2, 500),
    ]
    teams = []
    for index, (points, rank, fallback) in enumerate(entries[:size]):
        team = Team.objects.create(
            fpl_team_id=str(index + 1),
            team_name=f"Team {index}",
            current_total_points=fallback,
        )
        MiniLeagueTeam.objects.create(
            mini_league=league, team=team, total_points=points, current_rank=rank
        )
        teams.append(team)
    service = ProjectionService(
        context={"season": "test", "scoring_gameweek": gameweek}
    )
    cache.clear()
    try:
        for team in teams:
            full = service.get_team_mini_league_summary(
                team,
                league,
                standings=league.mini_league_teams.select_related("team").order_by(
                    "pk"
                ),
            )
            assert service.get_team_mini_league_summary(team, league) == full
        outsider = Team.objects.create(fpl_team_id="outsider", team_name="Outside")
        assert service.get_team_mini_league_summary(outsider, league) is None
    finally:
        cache.clear()
