import logging

from django.db import transaction
from django.utils import timezone

from rivals.models import MiniLeague, Team, UserMiniLeague

from .mini_league_sync_service import MiniLeagueSyncService

logger = logging.getLogger(__name__)


class InitUserService:
    def __init__(self, user):
        self.user = user

    def save_user_info(self, basic_info: dict) -> bool:
        if not basic_info or "leagues" not in basic_info:
            return False

        try:
            with transaction.atomic():
                user_team = self._create_user_team(basic_info)

                if not user_team:
                    return False

                current_leagues = self._process_classic_leagues(
                    basic_info.get("leagues", {}).get("classic", [])
                )

                if current_leagues:
                    current_league_ids = [ml.id for ml in current_leagues]
                    UserMiniLeague.objects.filter(user=self.user).exclude(
                        mini_league_id__in=current_league_ids
                    ).delete()

            # Attempt full team data sync (current_season, gameweeks, squad, transfers).
            # Handled in a separate try/except so non-critical sync errors do not fail
            # the user initialization after associations have been safely committed.
            try:
                from .team_sync_service import TeamSyncService

                TeamSyncService(user_team).sync_full_data()
            except Exception as sync_err:
                logger.warning(
                    f"Non-fatal error syncing full team data for team {user_team.fpl_team_id}: {sync_err}",
                    exc_info=True,
                )

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

    def _process_classic_leagues(self, classic_leagues: list[dict]) -> list[MiniLeague]:
        synced_leagues = []
        for league_data in classic_leagues:
            if self._is_system_league(league_data):
                continue

            if not league_data.get("rank_count") or league_data["rank_count"] <= 1:
                continue

            mini_league = self._create_or_update_mini_league(league_data)
            if not mini_league:
                continue

            self._create_user_league_association(mini_league)
            self._sync_league_data(mini_league)
            synced_leagues.append(mini_league)

        return synced_leagues

    def _is_system_league(self, league_data: dict) -> bool:
        system_short_names = ["overall", "region-", "event-", "brd-", "sc", "top-"]

        if league_data.get("league_type") == "s":
            return True

        short_name = league_data["short_name"] or ""
        if any(short_name.startswith(name) for name in system_short_names):
            return True

        if league_data.get("rank_count") and league_data["rank_count"] > 100000:
            return True

        return False

    def _create_or_update_mini_league(self, league_data: dict) -> MiniLeague | None:
        mini_league, created = MiniLeague.objects.get_or_create(
            fpl_league_id=str(league_data["id"]),
            defaults={
                "name": league_data["name"],
                "description": self._build_league_description(league_data),
                "league_type": league_data["league_type"],
                "scoring_type": league_data["scoring"],
                "has_cup": league_data.get("has_cup", False),
                "admin_entry_id": (
                    str(league_data["admin_entry"])
                    if league_data.get("admin_entry")
                    else None
                ),
                "start_event": league_data.get("start_event"),
                "entry_can_leave": league_data.get("entry_can_leave", False),
                "entry_can_admin": league_data.get("entry_can_admin", False),
                "entry_can_invite": league_data.get("entry_can_invite", False),
                "last_synced_at": timezone.now(),
            },
        )
        return mini_league

    def _build_league_description(self, league_data: dict) -> str:
        parts = []
        if "rank_count" in league_data:
            parts.append(f"{league_data['rank_count']} teams")
        if league_data.get("has_cup"):
            parts.append("Cup competition")
        if league_data.get("start_event") and league_data["start_event"] != 1:
            parts.append(f"Started GW{league_data['start_event']}")
        return " • ".join(parts)

    def _create_user_league_association(self, mini_league: MiniLeague) -> None:
        UserMiniLeague.objects.get_or_create(
            user=self.user,
            mini_league=mini_league,
            defaults={"active": True},
        )

    def _sync_league_data(self, mini_league: MiniLeague) -> None:
        sync_service = MiniLeagueSyncService(mini_league)
        sync_service.sync()
