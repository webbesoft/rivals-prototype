"""Editable best-effort planning values, not a claim of private FPL access."""

from collections import defaultdict

from django.utils.dateparse import parse_datetime

from rivals.models import TeamPlanningInput

FREE_CHIPS = {"wildcard", "freehit"}


class PlanningDefaults:
    def __init__(self, user, context, players=None, entry_history=None):
        self.user, self.team, self.context = user, user.team, context
        self.players = players or {}
        self.season = context["season"]
        self.target = context["decision_gameweek"]
        self.history = {
            row.gameweek: row
            for row in self.team.gameweek_data.filter(
                season=self.season, gameweek__lt=self.target
            )
        }
        self.transfers = list(
            self.team.transfers.filter(season=self.season, gameweek__lte=self.target)
            .select_related("player_in", "player_out")
            .order_by("transfer_time", "pk")
        )
        self.by_week = defaultdict(list)
        for transfer in self.transfers:
            self.by_week[transfer.gameweek].append(transfer)
        self.records = list(
            TeamPlanningInput.objects.filter(
                user=user, team=self.team, season=self.season, gameweek__lte=self.target
            ).order_by("-gameweek", "-updated_at")
        )
        self.entry_history = entry_history or {}

    def after(self, transfer, timestamp):
        return transfer.transfer_time is not None and transfer.transfer_time > timestamp

    def transfer_count(self, week, after=None):
        if after is not None:
            # Midweek corrections already include any earlier, possibly hidden moves.
            return sum(self.after(transfer, after) for transfer in self.by_week[week])
        history = self.history.get(week)
        return max(history.event_transfers if history else 0, len(self.by_week[week]))

    def free_transfers(self):
        anchor = next(
            (record for record in self.records if record.free_transfers is not None),
            None,
        )
        if anchor:
            balance = min(5, anchor.free_transfers)
            start = anchor.gameweek
            timestamp = anchor.free_transfers_recorded_at or anchor.updated_at
        else:
            balance = 1
            start = (self.team.started_event or 1) + 1
            timestamp = None
        for week in range(start, self.target + 1):
            row = self.history.get(week)
            chip = row.chip_name if row else None
            if not anchor and week < self.target and not row and not self.by_week[week]:
                # Missing history is not evidence of weeks of unused transfers.
                balance = 0
            if chip not in FREE_CHIPS:
                balance = max(
                    0,
                    balance
                    - self.transfer_count(
                        week, after=timestamp if anchor and week == start else None
                    ),
                )
            if week < self.target and chip not in FREE_CHIPS:
                balance = min(5, balance + 1)
        return balance

    def bank(self):
        anchor = next(
            (record for record in self.records if record.bank is not None), None
        )
        timestamp = (anchor.bank_recorded_at or anchor.updated_at) if anchor else None
        known = [
            row
            for row in self.history.values()
            if row.bank is not None and row.chip_name != "freehit"
        ]
        latest = max(known, key=lambda row: row.gameweek) if known else None
        deadlines = {
            event["id"]: parse_datetime(event.get("deadline_time", ""))
            for event in self.context.get("events", [])
        }
        if latest and (
            not anchor
            or (
                deadlines.get(latest.gameweek)
                and deadlines[latest.gameweek] > timestamp
            )
        ):
            balance, timestamp = latest.bank, deadlines.get(latest.gameweek)
        elif anchor:
            balance = int(anchor.bank * 10)
        elif self.entry_history.get("bank") is not None:
            balance = self.entry_history["bank"]
            timestamp = deadlines.get(self.context["scoring_gameweek"])
        else:
            balance = self.team.bank if self.team.current_season == self.season else 0
            latest_week = max(self.history, default=self.context["scoring_gameweek"])
            timestamp = deadlines.get(latest_week)
        if timestamp:
            for transfer in self.transfers:
                history = self.history.get(transfer.gameweek)
                if self.after(transfer, timestamp) and (
                    not history or history.chip_name != "freehit"
                ):
                    if (
                        transfer.player_in_cost is not None
                        and transfer.player_out_cost is not None
                    ):
                        balance += int(
                            transfer.player_out_cost - transfer.player_in_cost
                        )
        return max(0, int(balance))

    @staticmethod
    def player_id(transfer):
        if transfer.player_in:
            return transfer.player_in.fpl_id
        if transfer.player_in_name.startswith("Player #"):
            try:
                return int(transfer.player_in_name.removeprefix("Player #"))
            except ValueError:
                pass
        return None

    def selling_price(self, pid):
        player = self.players[pid]
        current = int(player.get("now_cost") or 0)
        record = next(
            (record for record in self.records if record.gameweek == self.target), None
        )
        if record and str(pid) in record.selling_prices:
            price = int(record.selling_prices[str(pid)])
            old_market = record.selling_price_markets.get(str(pid))
            if old_market is None:
                if 0 <= price <= current:
                    return price
            else:
                # Reconstruct the conservative purchase-price bound from the correction.
                purchase = (
                    max(0, 2 * price - int(old_market))
                    if price < old_market
                    else int(old_market)
                )
                return (
                    current
                    if current <= purchase
                    else purchase + (current - purchase) // 2
                )
        acquired = next(
            (
                transfer
                for transfer in reversed(self.transfers)
                if self.player_id(transfer) == pid
                and transfer.player_in_cost is not None
                and (
                    not self.history.get(transfer.gameweek)
                    or self.history[transfer.gameweek].chip_name != "freehit"
                )
            ),
            None,
        )
        if acquired:
            purchase = int(acquired.player_in_cost)
        else:
            # Opening price is a useful estimate when acquisition history is missing.
            purchase = current - int(player.get("cost_change_start") or 0)
        return max(
            0, current if current <= purchase else purchase + (current - purchase) // 2
        )

    def values(self, ids):
        return {
            "free_transfers": self.free_transfers(),
            "bank": self.bank(),
            "sales": {str(pid): self.selling_price(pid) for pid in ids},
        }
