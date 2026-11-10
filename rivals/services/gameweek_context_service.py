"""One deadline-aware interpretation of FPL events, usable without HTTP."""

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from rivals.models import SyncState


class GameweekContextService:
    @staticmethod
    def derive(events, fixtures=None, now=None):
        now = now or timezone.now()
        dated = []
        for event in events:
            deadline = parse_datetime(event.get("deadline_time", ""))
            if deadline and timezone.is_aware(deadline):
                dated.append((deadline, event))
        dated.sort(key=lambda item: item[0])
        passed = [(deadline, event) for deadline, event in dated if deadline <= now]
        future = [(deadline, event) for deadline, event in dated if deadline > now]
        scoring = passed[-1][1] if passed else None
        upcoming = future[0] if future else None
        season = None
        if dated:
            year = dated[0][0].year
            season = f"{year}/{(year + 1) % 100:02d}"
        active_match = any(
            fixture.get("started") and not fixture.get("finished")
            for fixture in fixtures or []
        )
        near_deadline = bool(
            upcoming and (upcoming[0] - now).total_seconds() <= 6 * 3600
        )
        just_passed = bool(passed and (now - passed[-1][0]).total_seconds() < 2 * 3600)
        return {
            "season": season,
            "scoring_gameweek": scoring["id"] if scoring else None,
            "decision_gameweek": upcoming[1]["id"] if upcoming else None,
            "next_deadline": upcoming[0] if upcoming else None,
            "latest_deadline": passed[-1][0] if passed else None,
            "finished": bool(scoring and scoring.get("finished")),
            "refresh_seconds": (
                120 if active_match or near_deadline or just_passed else 1800
            ),
        }

    @classmethod
    def cached(cls):
        state = SyncState.objects.filter(key="context").first()
        context = cls.derive(
            state.payload.get("events", []) if state else [],
            state.payload.get("fixtures", []) if state else [],
        )
        context["updated_at"] = state.last_success_at if state else None
        context["stale"] = (
            not state
            or not state.last_success_at
            or (timezone.now() - state.last_success_at).total_seconds() > 600
        )
        return context
