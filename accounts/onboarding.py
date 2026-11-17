"""One FPL readiness rule for browser, API and assistant authorization."""

from django.contrib.auth import get_user_model
from django.db import transaction

from rivals.services.init_user_service import InitUserService


def has_fpl_setup(user):
    return bool(
        user.fpl_id and user.team_id and str(user.team.fpl_team_id) == str(user.fpl_id)
    )


def save_fpl_setup(user, fpl_id, snapshot):
    """Publish the validated team and refresh jobs together, with no HTTP."""
    with transaction.atomic():
        current = get_user_model().objects.select_for_update().get(pk=user.pk)
        if current.fpl_revision != user.fpl_revision:
            raise ValueError("A newer team update has already completed.")
        if str(snapshot.get("id")) != str(fpl_id):
            raise ValueError("The FPL snapshot does not match this team.")
        current.fpl_id = str(fpl_id)
        current.team_name = snapshot["name"]
        current.manager_name = (
            f"{snapshot['player_first_name']} {snapshot['player_last_name']}"
        ).strip()
        current.active = True
        current.fpl_revision += 1
        current.save(
            update_fields=[
                "fpl_id",
                "team_name",
                "manager_name",
                "active",
                "fpl_revision",
            ]
        )
        if not InitUserService(current).save_user_info(
            snapshot, expected_revision=current.fpl_revision
        ):
            raise ValueError("Could not initialize the FPL team.")
        current.refresh_from_db()
        return current
