import logging

from django.db import transaction

from rivals.models import Team

from .league_membership_service import account_leagues, reconcile_memberships

logger = logging.getLogger(__name__)


class InitUserService:
    def __init__(self, user):
        self.user = user

    def save_user_info(self, basic_info: dict, *, expected_revision=None) -> bool:
        try:
            with transaction.atomic():
                self.user = (
                    type(self.user).objects.select_for_update().get(pk=self.user.pk)
                )
                if expected_revision is not None and (
                    self.user.fpl_revision != expected_revision
                    or str(basic_info.get("id")) != str(self.user.fpl_id)
                ):
                    # An older initialization job is successfully revoked.
                    return True
                account_leagues(basic_info)
                user_team = self._create_user_team(basic_info)

                if not user_team:
                    return False

                reconcile_memberships(self.user, basic_info)

                # Queue database work atomically; workers perform the HTTP requests.
                from .refresh_service import enqueue_user

                enqueue_user(self.user)

            return True

        except Exception as e:
            logger.error(f"InitUserService Error: {e}", exc_info=True)
            return False

    def _create_user_team(self, basic_info: dict) -> Team | None:
        team, created = Team.objects.get_or_create(
            fpl_team_id=str(basic_info["id"]),
            defaults={
                "team_name": basic_info["name"],
                "manager_name": f"{basic_info['player_first_name']} {basic_info['player_last_name']}".strip(),
                "current_total_points": basic_info["summary_overall_points"],
                "current_overall_rank": basic_info["summary_overall_rank"],
            },
        )
        if not created:
            team.team_name = basic_info["name"]
            team.manager_name = f"{basic_info['player_first_name']} {basic_info['player_last_name']}".strip()
            team.current_total_points = basic_info.get(
                "summary_overall_points", team.current_total_points
            )
            team.current_overall_rank = basic_info.get(
                "summary_overall_rank", team.current_overall_rank
            )
            team.save(
                update_fields=[
                    "team_name",
                    "manager_name",
                    "current_total_points",
                    "current_overall_rank",
                ]
            )

        self.user.team = team
        self.user.save(update_fields=["team"])
        return team
