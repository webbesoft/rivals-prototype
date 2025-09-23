import logging
from decimal import Decimal

import requests
from django.db import transaction
from django.utils.dateparse import parse_datetime

from rivals.models import Player

logger = logging.getLogger(__name__)


class PlayerSyncService:
    def __init__(self):
        self.new_count = 0
        self.updated_count = 0
        self.errors = []

    def sync_all_players(self) -> dict:
        players_data = self._fetch_players_data()

        if not players_data:
            return {"success": False, "error": "Failed to fetch player data"}

        try:
            with transaction.atomic():
                for player_data in players_data:
                    self._sync_player(player_data)

            return {
                "success": True,
                "updated": self.new_count + self.updated_count,
                "new_count": self.new_count,
                "updated_count": self.updated_count,
                "errors": self.errors,
            }

        except Exception as e:
            logger.error(f"Player sync failed: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    def _fetch_players_data(self) -> list | None:
        try:
            response = requests.get(
                "https://fantasy.premierleague.com/api/elements/", timeout=10
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"Failed to fetch players: {e}")
            return None

    def _sync_player(self, player_data: dict) -> None:
        try:
            defaults = {
                "web_name": player_data["web_name"],
                "first_name": player_data["first_name"],
                "second_name": player_data["second_name"],
                "element_type": player_data["element_type"],
                "team_id": player_data["team"],
                "now_cost": player_data["now_cost"],
                "total_points": player_data["total_points"],
                "status": player_data["status"],
                "selected_by_percent": player_data["selected_by_percent"],
                "form": player_data["form"],
                "photo": player_data["photo"],
                "can_transact": player_data.get("can_transact", False),
                "removed": player_data.get("removed", False),
                "news": player_data["news"],
                "news_added": (
                    parse_datetime(player_data["news_added"])
                    if player_data.get("news_added")
                    else None
                ),
                "minutes": player_data["minutes"],
                "goals_scored": player_data["goals_scored"],
                "assists": player_data["assists"],
                "clean_sheets": player_data["clean_sheets"],
                "goals_conceded": player_data["goals_conceded"],
                "yellow_cards": player_data["yellow_cards"],
                "red_cards": player_data["red_cards"],
                "bonus": player_data["bonus"],
                "expected_goals": Decimal(player_data["expected_goals"]),
                "expected_assists": Decimal(player_data["expected_assists"]),
                "ict_index": Decimal(player_data["ict_index"]),
            }

            player, created = Player.objects.update_or_create(
                fpl_id=player_data["id"], defaults=defaults
            )

            if created:
                self.new_count += 1
            else:
                self.updated_count += 1

        except Exception as e:
            error_msg = f"Player {player_data.get('web_name', '?')}: {e}"
            self.errors.append(error_msg)
            logger.error(error_msg)
