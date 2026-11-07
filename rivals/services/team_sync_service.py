import logging

from django.utils import timezone

from rivals.models import (
    GameweekData,
    Player,
    SeasonHistory,
    SquadHistory,
    Team,
    Transfer,
)
from rivals.services.fpl_api_service import FplApiService

logger = logging.getLogger(__name__)


class TeamSyncService:
    def __init__(self, team: Team):
        self.team = team
        self.fpl_api_service = FplApiService()
        self._history_data: dict | None = None
        self._player_names_cache: dict[int, str | None] = {}

    def sync_full_data(self) -> bool:
        try:
            # Detect a season change before syncing any data
            self.detect_and_handle_season_transition()

            self.sync_basic_info()
            history_data = self.sync_history()
            self.sync_transfers()
            self.sync_squad_histories(history_data=history_data)
            self.team.last_synced_at = timezone.now()
            self.team.save(update_fields=["last_synced_at"])
            return True
        except Exception as e:
            logger.error(f"Team Sync Error: {e}", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Season transition
    # ------------------------------------------------------------------

    def detect_and_handle_season_transition(self) -> bool:
        """
        Compares the live FPL season against the team's recorded season.
        If they differ, the current-season counters are reset so we start fresh.
        The old gameweek/transfer/squad data is preserved via the `season` field.

        Returns True if a transition was detected, False otherwise.
        """
        live_season = self.fpl_api_service.fetch_current_season()
        if not live_season:
            logger.warning(
                "Could not fetch current season — skipping transition check."
            )
            return False

        # Stamp existing records that have no season so history is not lost
        if not self.team.current_season:
            self._backfill_season(live_season)
            self.team.current_season = live_season
            self.team.save(update_fields=["current_season"])
            return False

        if self.team.current_season == live_season:
            return False  # Same season, nothing to do

        logger.info(
            f"Season transition detected for team {self.team.fpl_team_id}: "
            f"{self.team.current_season} → {live_season}"
        )

        # Reset current-season aggregates
        self.team.current_total_points = 0
        self.team.current_overall_rank = None
        self.team.current_event_points = 0
        self.team.bank = 0
        self.team.current_season = live_season
        self.team.save(
            update_fields=[
                "current_total_points",
                "current_overall_rank",
                "current_event_points",
                "bank",
                "current_season",
            ]
        )
        return True

    def _backfill_season(self, live_season: str) -> None:
        """
        Stamp existing season-less records with their correct season label.

        When a team has no current_season yet (first sync), we try to derive the
        season from the most recent SeasonHistory entry, which reflects the last
        completed FPL season stored via FPL history.past.  Falling back to
        last_synced_at lets us label records that pre-date the season label
        migration with a best-effort estimate without incorrectly attributing
        old data to the brand-new live season.

        The live_season is used only when no historical signal is available.
        """
        # Prefer the most recently completed FPL season from stored history
        latest_history = self.team.season_histories.order_by("-season_name").first()
        if latest_history:
            backfill_label = latest_history.season_name
        elif self.team.last_synced_at:
            # Records exist from a previous sync; assume they belong to live_season
            # (best effort — the team was new when last seen)
            backfill_label = live_season
        else:
            backfill_label = live_season

        GameweekData.objects.filter(team=self.team, season__isnull=True).update(
            season=backfill_label
        )
        Transfer.objects.filter(team=self.team, season__isnull=True).update(
            season=backfill_label
        )
        SquadHistory.objects.filter(team=self.team, season__isnull=True).update(
            season=backfill_label
        )

    # ------------------------------------------------------------------
    # Standard sync methods
    # ------------------------------------------------------------------

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

    def sync_history(self) -> dict | None:
        if self._history_data is None:
            self._history_data = self.fpl_api_service.fetch_team_history(
                self.team.fpl_team_id
            )
        data = self._history_data
        if not data or "current" not in data:
            return None

        current_season = self.team.current_season

        for gw in data["current"]:
            gw_data, _ = GameweekData.objects.get_or_create(
                team=self.team,
                gameweek=gw["event"],
                season=current_season,
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

        if data["current"]:
            self.team.bank = data["current"][-1]["bank"]
            self.team.save(update_fields=["bank"])

        # Sync past-season summaries
        for past in data.get("past", []):
            SeasonHistory.objects.update_or_create(
                team=self.team,
                season_name=past["season_name"],
                defaults={
                    "total_points": past["total_points"],
                    "overall_rank": past.get("rank"),
                    "rank": past.get("rank"),
                },
            )
        return data

    def sync_transfers(self) -> None:
        transfers = self.fpl_api_service.fetch_team_transfers(self.team.fpl_team_id)
        if not transfers:
            return

        current_season = (
            self.team.current_season or self.fpl_api_service.fetch_current_season()
        )
        if not self.team.current_season and current_season:
            self.team.current_season = current_season
            self.team.save(update_fields=["current_season"])

        # Build de-dup set based on (gameweek, iso_timestamp)
        existing_signatures = set()
        for gw, tt in Transfer.objects.filter(team=self.team).values_list(
            "gameweek", "transfer_time"
        ):
            if tt:
                existing_signatures.add(
                    (gw, tt.isoformat() if hasattr(tt, "isoformat") else str(tt))
                )

        new_transfers_data = []
        needed_elements = set()
        from django.utils.dateparse import parse_datetime

        for t in transfers:
            t_time = parse_datetime(t["time"]) if t.get("time") else None
            t_iso = t_time.isoformat() if t_time else (t.get("time") or "")
            sig = (t["event"], t_iso)
            if sig not in existing_signatures:
                existing_signatures.add(sig)
                new_transfers_data.append((t, t_time))
                needed_elements.add(t["element_in"])
                needed_elements.add(t["element_out"])

        if not new_transfers_data:
            return

        players_by_fpl_id = {
            p.fpl_id: p for p in Player.objects.filter(fpl_id__in=needed_elements)
        }

        # If any player elements are missing from DB, trigger quick player sync
        missing_ids = needed_elements - set(players_by_fpl_id.keys())
        if missing_ids:
            try:
                from rivals.services.player_sync_service import PlayerSyncService

                PlayerSyncService(self.fpl_api_service).sync_all_players()
                players_by_fpl_id = {
                    p.fpl_id: p
                    for p in Player.objects.filter(fpl_id__in=needed_elements)
                }
            except Exception as player_err:
                logger.warning(f"Failed to auto-sync missing players: {player_err}")

        transfers_to_create = []
        for t, t_time in new_transfers_data:
            player_in = players_by_fpl_id.get(t["element_in"])
            player_out = players_by_fpl_id.get(t["element_out"])

            transfers_to_create.append(
                Transfer(
                    team=self.team,
                    gameweek=t["event"],
                    season=current_season,
                    player_in_name=(
                        player_in.web_name
                        if player_in
                        else f"Player #{t['element_in']}"
                    ),
                    player_out_name=(
                        player_out.web_name
                        if player_out
                        else f"Player #{t['element_out']}"
                    ),
                    player_in=player_in,
                    player_out=player_out,
                    player_in_cost=t.get("element_in_cost"),
                    player_out_cost=t.get("element_out_cost"),
                    transfer_time=t_time,
                )
            )

        if transfers_to_create:
            Transfer.objects.bulk_create(transfers_to_create, ignore_conflicts=True)

    def sync_gameweek_picks(self, gameweek: int) -> None:
        picks = self.fpl_api_service.fetch_team_picks(self.team.fpl_team_id, gameweek)
        if not picks or "picks" not in picks:
            return

        gw_data = GameweekData.objects.filter(
            team=self.team,
            gameweek=gameweek,
            season=self.team.current_season,
        ).first()
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

    def sync_squad_histories(self, history_data: dict | None = None) -> None:
        data = history_data or self._history_data
        if not data:
            data = self.fpl_api_service.fetch_team_history(self.team.fpl_team_id)
            self._history_data = data
        if not data or "current" not in data:
            return

        gameweeks = [gw["event"] for gw in data["current"]]
        for gw in gameweeks:
            self.sync_squad_for_gameweek(gw)

    def sync_squad_for_gameweek(self, gameweek: int) -> None:
        if SquadHistory.objects.filter(
            team=self.team,
            gameweek=gameweek,
            season=self.team.current_season,
        ).exists():
            return

        picks = self.fpl_api_service.fetch_team_picks(self.team.fpl_team_id, gameweek)
        if not picks or "picks" not in picks:
            return

        elements = [p["element"] for p in picks["picks"]]
        players_by_fpl_id = {
            p.fpl_id: p for p in Player.objects.filter(fpl_id__in=elements)
        }

        squad_records = []
        for pick in picks["picks"]:
            player = players_by_fpl_id.get(pick["element"])
            if not player:
                logger.warning(f"Player not found for FPL ID {pick['element']}")
                continue

            squad_records.append(
                SquadHistory(
                    team=self.team,
                    gameweek=gameweek,
                    season=self.team.current_season,
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
            )

        if squad_records:
            SquadHistory.objects.bulk_create(squad_records, ignore_conflicts=True)

    def get_player_name(self, fpl_id: int) -> str | None:
        if fpl_id not in self._player_names_cache:
            player = Player.objects.filter(fpl_id=fpl_id).first()
            self._player_names_cache[fpl_id] = player.web_name if player else None
        return self._player_names_cache[fpl_id]
