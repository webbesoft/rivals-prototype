"""Turn official FPL threshold progress into cautious, next-update warnings."""

import math

from django.utils import timezone
from django.utils.dateparse import parse_datetime

PRICE_FIELDS = (
    "price_change_percent",
    "price_change_projections",
    "price_change_locked_until",
    "price_change_calibrating",
)


def percent(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def price_warning(player):
    if player.get("price_change_calibrating"):
        return None
    locked = player.get("price_change_locked_until")
    if locked:
        try:
            expiry = parse_datetime(locked)
        except (TypeError, ValueError):
            return None
        if not expiry or timezone.is_naive(expiry) or expiry > timezone.now():
            return None
    current = percent(player.get("price_change_percent"))
    projection = next(
        (
            item
            for item in (player.get("price_change_projections") or [])
            if isinstance(item, dict) and item.get("offset") == 0
        ),
        {},
    )
    projected = percent(projection.get("projected_percent"))
    progress = projected if projected is not None else current
    # 90% is our watch threshold, not an official likelihood category.
    if progress is None or abs(progress) < 90:
        return None
    direction = "rise" if progress > 0 else "fall"
    label = (
        f"Price {direction} projected"
        if projected is not None and abs(projected) >= 100
        else f"Near price {direction}"
    )
    measure = "Projected" if projected is not None else "Current"
    return {
        "direction": direction,
        "label": label,
        "current_percent": current,
        "projected_percent": projected,
        "detail": (
            f"{measure} progress towards a price {direction}: {abs(progress):.1f}% "
            "of the threshold for the next price update. This is not a probability "
            "or a guaranteed change."
        ),
    }


def transfer_timing(option, players):
    """Explain price exposure without inventing future prices or selling proceeds."""
    notes = []
    rises = 0
    for move in option["moves"]:
        for side, action in (("in", "buy"), ("out", "sell")):
            warning = price_warning(players[move[side]])
            if not warning:
                continue
            name = move[f"{side}_name"]
            direction = warning["direction"]
            if action == "buy":
                effect = (
                    "Waiting could make this purchase more expensive."
                    if direction == "rise"
                    else "Waiting could make this purchase cheaper."
                )
                rises += direction == "rise"
            else:
                effect = (
                    "Waiting could reduce the selling price; check your actual FPL selling value."
                    if direction == "fall"
                    else "A rise may improve the selling price, depending on your purchase price."
                )
            notes.append(f"{name}: {warning['label'].lower()}. {effect}")
    bank = option.get("bank_after")
    if rises and bank is not None and round(bank * 10) < rises:
        qualification = (
            " (using unconfirmed selling prices)"
            if "Confirm selling prices" in option.get("unknown", [])
            else ""
        )
        notes.append(
            f"Budget risk: a £0.1m rise for each flagged purchase would exceed "
            f"the estimated £{bank:.1f}m left after this route{qualification}."
        )
    return notes
