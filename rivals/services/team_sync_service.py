import logging

from django.utils import timezone

from rivals.models import GameweekData, Player, SquadHistory, Team, Transfer
from rivals.services.fpl_api_service import FplApiService

logger = logging.getLogger(__name__)


class TeamSyncService:
    def __init__(self, team: Team):
        self.team = team
        self.fpl_api_service = FplApiService()

    def sync_full_data(self) -> bool:
        try:
            self.sync_basic_info()
            self.sync_history()
            self.sync_transfers()
            self.sync_squad_histories()
            self.team.last_synced_at = timezone.now()
            self.team.save(update_fields=["last_synced_at"])
            return True
        except Exception as e:
            logger.error(f"Team Sync Error: {e}", exc_info=True)
            return False

    def sync_basic_info(self) -> None:
        data = self.fpl_api_service.fetch_team_basic_info(self.team.fpl_team_id)
        if not data:
            return
        self.team.team_name = data["name"]
        self.team.manager_name = (
            f"{data['player_first_name']} {data['player_last_name']}".strip()
        )
        self.team.current_total_points = data["summary_overall_points"]
        self.team.current_overall_rank = data["summary_overall_rank"]
        self.team.current_event_points = data["summary_event_points"]
        self.team.total_transfers = data["last_deadline_total_transfers"]
        self.team.save(
            update_fields=[
                "team_name",
                "manager_name",
                "current_total_points",
                "current_overall_rank",
                "current_event_points",
                "total_transfers",
            ]
        )

    def sync_history(self) -> None:
        data = self.fpl_api_service.fetch_team_history(self.team.fpl_team_id)
        if not data or "current" not in data:
            return

        for gw in data["current"]:
            gw_data, _ = GameweekData.objects.get_or_create(
                team=self.team, gameweek=gw["event"]
            )
            gw_data.points = gw["points"]
            gw_data.total_points = gw["total_points"]
            gw_data.rank = gw.get("rank")
            gw_data.rank_sort = gw.get("rank_sort")
            gw_data.event_transfers = gw["event_transfers"]
            gw_data.event_transfers_cost = gw["event_transfers_cost"]
            gw_data.bench_points = gw["points_on_bench"]
            gw_data.save()

            self.sync_gameweek_picks(gw["event"])

        self.team.bank = data["current"][-1]["bank"]
        self.team.save(update_fields=["bank"])

    def sync_transfers(self) -> None:
        transfers = self.fpl_api_service.fetch_team_transfers(self.team.fpl_team_id)
        if not transfers:
            return

        for t in transfers:
            exists = Transfer.objects.filter(
                team=self.team,
                gameweek=t["event"],
                player_in__fpl_id=t["element_in"],
                player_out__fpl_id=t["element_out"],
            ).exists()

            if exists:
                continue

            player_in = Player.objects.filter(fpl_id=t["element_in"]).first()
            player_out = Player.objects.filter(fpl_id=t["element_out"]).first()

            Transfer.objects.create(
                team=self.team,
                gameweek=t["event"],
                player_in_name=(
                    f"{player_in.first_name} {player_in.second_name}"
                    if player_in
                    else "Unavailable"
                ),
                player_out_name=(
                    f"{player_out.first_name} {player_out.second_name}"
                    if player_out
                    else "Unavailable"
                ),
                player_in=player_in,
                player_out=player_out,
                player_in_cost=t["element_in_cost"],
                player_out_cost=t["element_out_cost"],
                transfer_time=t["time"],
            )

    def sync_gameweek_picks(self, gameweek: int) -> None:
        picks = self.fpl_api_service.fetch_team_picks(self.team.fpl_team_id, gameweek)
        if not picks or "picks" not in picks:
            return

        gw_data = GameweekData.objects.filter(team=self.team, gameweek=gameweek).first()
        if not gw_data:
            return

        captain = next((p for p in picks["picks"] if p["is_captain"]), None)
        vice = next((p for p in picks["picks"] if p["is_vice_captain"]), None)

        gw_data.captain_name = (
            self.get_player_name(captain["element"]) if captain else None
        )
        gw_data.vice_captain_name = (
            self.get_player_name(vice["element"]) if vice else None
        )
        gw_data.chip_played = bool(picks.get("active_chip"))
        gw_data.chip_name = picks.get("active_chip")
        gw_data.save(
            update_fields=[
                "captain_name",
                "vice_captain_name",
                "chip_played",
                "chip_name",
            ]
        )

    def sync_squad_histories(self) -> None:
        data = self.fpl_api_service.fetch_team_history(self.team.fpl_team_id)
        if not data or "current" not in data:
            return

        gameweeks = [gw["event"] for gw in data["current"]]
        for gw in gameweeks:
            self.sync_squad_for_gameweek(gw)

    def sync_squad_for_gameweek(self, gameweek: int) -> None:
        if SquadHistory.objects.filter(team=self.team, gameweek=gameweek).exists():
            return

        picks = self.fpl_api_service.fetch_team_picks(self.team.fpl_team_id, gameweek)
        if not picks or "picks" not in picks:
            return

        for pick in picks["picks"]:
            player = Player.objects.filter(fpl_id=pick["element"]).first()
            if not player:
                logger.warning(f"Player not found for FPL ID {pick['element']}")
                continue

            SquadHistory.objects.create(
                team=self.team,
                gameweek=gameweek,
                player_fpl_id=pick["element"],
                position=pick["position"],
                multiplier=pick["multiplier"],
                is_captain=pick["is_captain"],
                is_vice_captain=pick["is_vice_captain"],
                element_type=pick["element_type"],
                player_name=player.web_name,
                player_cost=player.now_cost,
                form=player.form,
                expected_goals=player.expected_goals,
                expected_assists=player.expected_assists,
                selected_by_percent=player.selected_by_percent,
                total_points_at_time=player.total_points,
            )

    def get_player_name(self, fpl_id: int) -> str | None:
        player = Player.objects.filter(fpl_id=fpl_id).first()
        return player.web_name if player else None
