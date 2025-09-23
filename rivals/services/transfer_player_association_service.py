import logging

from django.db import models

from rivals.models import Player, Transfer

logger = logging.getLogger(__name__)


class TransferPlayerAssociationService:
    def associate_all_transfers(self) -> dict:
        associated_count = 0
        unmatched_count = 0

        # Iterate in chunks for memory efficiency
        queryset = Transfer.objects.select_related("player_in", "player_out").iterator(
            chunk_size=100
        )

        for transfer in queryset:
            updated = False

            if not transfer.player_in and transfer.player_in_name:
                player = self.find_player_by_name(transfer.player_in_name)
                if player:
                    transfer.player_in = player
                    updated = True

            if not transfer.player_out and transfer.player_out_name:
                player = self.find_player_by_name(transfer.player_out_name)
                if player:
                    transfer.player_out = player
                    updated = True

            if updated:
                transfer.save(update_fields=["player_in", "player_out"])
                associated_count += 1
            elif not transfer.player_in or not transfer.player_out:
                unmatched_count += 1

        return {
            "associated_count": associated_count,
            "unmatched_count": unmatched_count,
        }

    def find_player_by_name(self, name: str) -> Player | None:
        """
        Match by web_name first, then try first+second name.
        """
        try:
            # Exact web_name match
            player = Player.objects.filter(web_name=name).first()
            if player:
                return player

            return (
                Player.objects.annotate(
                    full_name=models.functions.Concat(
                        "first_name", models.Value(" "), "second_name"
                    )
                )
                .filter(full_name__icontains=name)
                .first()
            )
        except Exception as e:
            logger.error(f"Error finding player by name '{name}': {e}", exc_info=True)
            return None
