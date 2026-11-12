"""Small, independently refreshed score snapshots; never sync squad history."""

from django.core.cache import cache
from django.utils import timezone

from rivals.services.fpl_api_service import FplApiService


class ScoreSummaryService:
    FRESH_SECONDS = 120
    RETAIN_SECONDS = 7 * 24 * 60 * 60
    RETRY_SECONDS = 30

    def __init__(self, team):
        self.team = team
        self.key = f"score_summary_v1_{team.fpl_team_id}"

    def cached(self):
        snapshot = cache.get(self.key)
        if snapshot:
            return {
                **snapshot,
                "stale": (timezone.now() - snapshot["updated_at"]).total_seconds()
                >= self.FRESH_SECONDS,
            }
        return None

    def refresh(self):
        snapshot = self.cached()
        if snapshot and not snapshot["stale"]:
            return snapshot
        # Atomic cooldown limits concurrent requests and retries during outages.
        if not cache.add(f"{self.key}_attempt", True, self.RETRY_SECONDS):
            return self._unavailable(snapshot)

        api = FplApiService(snapshot_only=True)
        api.TIMEOUT = 5
        bootstrap = api.fetch_bootstrap_static()
        if not bootstrap or not bootstrap.get("events"):
            return self._unavailable(snapshot)
        entry = api.fetch_team_basic_info(self.team.fpl_team_id)
        if not entry:
            return self._unavailable(snapshot)

        summary = self._summary(bootstrap, entry, timezone.now())
        if summary is None:
            return self._unavailable(snapshot)
        cache.set(self.key, summary, self.RETAIN_SECONDS)
        return summary

    @staticmethod
    def _summary(bootstrap, entry, updated_at):
        # Pair the entry's score with its own event, including during rollovers.
        event_id = entry.get("current_event")
        event = next(
            (event for event in bootstrap["events"] if event["id"] == event_id),
            None,
        )
        if event_id is not None and event is None:
            return None
        points = entry.get("summary_event_points") if event else None
        average = event.get("average_entry_score") if event else None
        summary = {
            "gameweek": event_id,
            "points": points,
            "average": average,
            "difference": (
                points - average if points is not None and average is not None else None
            ),
            "total_points": entry.get("summary_overall_points"),
            "overall_rank": entry.get("summary_overall_rank"),
            "finished": bool(event and event.get("finished")),
            "updated_at": updated_at,
            "stale": False,
            "unavailable": False,
        }
        return summary

    def _saved_summary(self):
        """Keep worker publications useful across web restarts and cache misses."""
        from rivals.models import FplSnapshot
        from rivals.services.fpl_snapshot_service import request_snapshot
        from rivals.services.gameweek_context_service import GameweekContextService

        cutoff = timezone.now() - timezone.timedelta(seconds=self.RETAIN_SECONDS)
        bootstrap = FplSnapshot.objects.filter(
            key="bootstrap", updated_at__gte=cutoff
        ).first()
        if not bootstrap or not bootstrap.payload.get("events"):
            return None
        season = GameweekContextService.derive(bootstrap.payload["events"])["season"]
        current_season = GameweekContextService.cached()["season"]
        if not season or (current_season and season != current_season):
            return None
        key = f"profile:{season}:{self.team.fpl_team_id}"
        entry = FplSnapshot.objects.filter(key=key, updated_at__gte=cutoff).first()
        if not entry or entry.expires_at <= timezone.now():
            request_snapshot(key)
        if not entry or not entry.payload:
            return None
        return self._summary(
            bootstrap.payload,
            entry.payload,
            min(bootstrap.updated_at, entry.updated_at),
        )

    def _unavailable(self, snapshot):
        snapshot = snapshot or self._saved_summary()
        if snapshot:
            return {**snapshot, "stale": True, "unavailable": True}
        return {"unavailable": True, "stale": True, "refreshing": True}
