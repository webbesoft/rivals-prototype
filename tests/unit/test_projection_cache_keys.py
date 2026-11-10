from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
import warnings

import pytest
from django.core.cache import cache
from django.core.cache.backends.base import CacheKeyWarning

from rivals.services.projection_service import ProjectionService


@pytest.mark.parametrize(
    "synced_at", [None, datetime(2026, 11, 10, 16, 10, 27, 975855, tzinfo=timezone.utc)]
)
def test_league_projection_cache_is_portable_and_invalidates_after_sync(synced_at):
    team = Mock(
        id=1, team_name="My team", manager_name="Manager", current_total_points=500
    )
    standing = Mock(team=team, total_points=500, current_rank=1)
    league = Mock(id=1, last_synced_at=synced_at)
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
