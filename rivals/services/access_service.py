"""Shared access and tracking rules for browser and API entry points."""

from django.contrib.auth import get_user_model
from django.db import transaction

from rivals.models import MiniLeague, TrackedTeam


def accessible_leagues(user):
    return MiniLeague.objects.filter(
        active=True, user_minileagues__user=user, user_minileagues__active=True
    )


class TrackingLimitReached(ValueError):
    pass


def track_team(user, team):
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=user.pk)
        tracked = TrackedTeam.objects.filter(user=user, team=team).first()
        if tracked and tracked.active:
            return
        if not user.can_track_team:
            raise TrackingLimitReached("You can track up to 10 teams.")
        TrackedTeam.objects.update_or_create(
            user=user, team=team, defaults={"active": True}
        )
