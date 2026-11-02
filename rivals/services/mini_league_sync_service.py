import logging

from django.utils import timezone

from rivals.models import MiniLeague, Team

from .fpl_api_service import FplApiService

logger = logging.getLogger(__name__)


class MiniLeagueSyncService:
    def __init__(self, mini_league: MiniLeague):
        self.mini_league = mini_league
        self.fpl_api_service = FplApiService()

    def sync(self) -> bool:
        """
        Fetch league info + standings from FPL API,
        update the MiniLeague and associated teams.
        """
        try:
            league_data = self.fpl_api_service.fetch_mini_league(
                self.mini_league.fpl_league_id
            )

            if not league_data:
                return False

            # --- Update league info ---
            league_info = league_data["league_info"]
            self.mini_league.name = league_info["name"]
            self.mini_league.last_synced_at = timezone.now()
            self.mini_league.save(update_fields=["name", "last_synced_at"])

            # --- Sync teams and standings ---
            standings = league_data["standings"]
            self._sync_teams_and_standings(standings)

            return True

        except Exception as e:
            logger.error(f"Mini League Sync Error: {e}", exc_info=True)
            return False

    def _sync_teams_and_standings(self, standings: list[dict]) -> None:
        """
        Update or create teams in the league and track standings.
        """
        current_season = self.fpl_api_service.fetch_current_season()
        existing_mlts = {
            mlt.team_id: mlt
            for mlt in self.mini_league.mini_league_teams.all()
        }

        for standing in standings:
            team = self._find_or_create_team(standing)

            if not team:
                continue

            mini_league_team = existing_mlts.get(team.id)

            if mini_league_team:
                season_changed = (
                    current_season is not None
                    and mini_league_team.season != current_season
                )
                previous_rank = mini_league_team.current_rank
                mini_league_team.current_rank = standing["rank"]
                mini_league_team.total_points = standing["total"]
                if season_changed:
                    # New season: keep the current rank but discard the stale
                    # previous_rank from the old season so it is not displayed.
                    mini_league_team.previous_rank = None
                    mini_league_team.season = current_season
                    mini_league_team.save(
                        update_fields=["current_rank", "previous_rank", "total_points", "season"]
                    )
                else:
                    mini_league_team.previous_rank = previous_rank
                    update_fields = ["current_rank", "previous_rank", "total_points"]
                    if current_season and not mini_league_team.season:
                        mini_league_team.season = current_season
                        update_fields.append("season")
                    mini_league_team.save(update_fields=update_fields)
            else:
                new_mlt = self.mini_league.mini_league_teams.create(
                    team=team,
                    season=current_season,
                    current_rank=standing["rank"],
                    previous_rank=None,
                    total_points=standing["total"],
                )
                existing_mlts[team.id] = new_mlt

    def _find_or_create_team(self, standing: dict) -> Team | None:
        """
        Create or update Team records from standings data.
        """
        try:
            team, created = Team.objects.get_or_create(
                fpl_team_id=str(standing["entry"]),
                defaults={
                    "team_name": standing["entry_name"],
                    "manager_name": standing["player_name"],
                    "current_total_points": standing["total"],
                },
            )

            if not created:
                # Keep team info in sync
                team.team_name = standing["entry_name"]
                team.manager_name = standing["player_name"]
                team.current_total_points = standing["total"]
                team.save(
                    update_fields=["team_name", "manager_name", "current_total_points"]
                )

            return team

        except Exception as e:
            logger.error(f"Team creation error: {e}", exc_info=True)
            return None
