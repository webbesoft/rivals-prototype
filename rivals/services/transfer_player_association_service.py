"""Resolve bounded transfer batches using only identities from the same season."""

from django.db.models import Q

from rivals.models import Player, Transfer


class TransferPlayerAssociationService:
    BATCH_SIZE = 500

    def associate_all_transfers(self, season=None, *, after_pk=0):
        if not season:
            return {"associated_count": 0, "unmatched_count": 0, "next_cursor": 0}
        rows = list(
            Transfer.objects.filter(season=season, pk__gt=after_pk)
            .filter(Q(player_in__isnull=True) | Q(player_out__isnull=True))
            .order_by("pk")[: self.BATCH_SIZE + 1]
        )
        more = len(rows) > self.BATCH_SIZE
        associated = unmatched = 0
        for transfer in rows[: self.BATCH_SIZE]:
            changed = []
            for side in ("in", "out"):
                field = f"player_{side}"
                if getattr(transfer, field + "_id"):
                    continue
                element = getattr(transfer, field + "_fpl_id")
                name = getattr(transfer, field + "_name")
                player = (
                    Player.objects.filter(season=season, fpl_id=element).first()
                    if element
                    else self.find_player_by_name(name, season)
                )
                if player:
                    setattr(transfer, field, player)
                    setattr(transfer, field + "_fpl_id", player.fpl_id)
                    changed.extend([field, field + "_fpl_id"])
                    if not name or name.startswith("Player #"):
                        setattr(transfer, field + "_name", player.web_name)
                        changed.append(field + "_name")
            if changed:
                transfer.save(update_fields=changed)
                associated += 1
            if not transfer.player_in_id or not transfer.player_out_id:
                unmatched += 1
        return {
            "associated_count": associated,
            "unmatched_count": unmatched,
            "next_cursor": rows[self.BATCH_SIZE - 1].pk if more else 0,
        }

    def find_player_by_name(self, name, season=None):
        if not season or not name:
            return None
        players = Player.objects.filter(season=season)
        if name.startswith("Player #"):
            try:
                return players.filter(fpl_id=int(name.removeprefix("Player #"))).first()
            except ValueError:
                return None
        # Ambiguous display names must remain unresolved.
        matches = list(players.filter(web_name=name)[:2])
        return matches[0] if len(matches) == 1 else None
