"""A manager's next-deadline XI, distinct from an optimised forecast."""

from collections import Counter


def validate_lineup(lineup, squad, players):
    if not lineup:
        return
    if not isinstance(lineup, dict):
        raise ValueError("Choose a valid planned lineup.")
    xi = lineup.get("xi", [])
    if isinstance(xi, list) and len(xi) != 11:
        difference = abs(len(xi) - 11)
        action = "remove" if len(xi) > 11 else "add"
        raise ValueError(
            f"You selected {len(xi)} starters. Select exactly 11: {action} "
            f"{difference} player{'s' if difference != 1 else ''}."
        )
    if (
        not isinstance(xi, list)
        or len(xi) != 11
        or any(type(pid) is not int for pid in xi)
        or len(set(xi)) != 11
        or not set(xi).issubset(squad)
    ):
        raise ValueError("Choose 11 different starters from your current squad.")
    counts = Counter(players[pid]["element_type"] for pid in xi)
    for position, label, minimum, maximum in (
        (1, "goalkeeper", 1, 1),
        (2, "defender", 3, 5),
        (3, "midfielder", 2, 5),
        (4, "forward", 1, 3),
    ):
        count = counts[position]
        if not minimum <= count <= maximum:
            limit = (
                "exactly 1"
                if minimum == maximum
                else (
                    f"at least {minimum}"
                    if count < minimum
                    else f"no more than {maximum}"
                )
            )
            raise ValueError(
                f"You selected {count} {label}{'s' if count != 1 else ''}. Start {limit} {label}{'s' if minimum != maximum else ''}."
            )
    captain, vice = lineup.get("captain"), lineup.get("vice")
    if captain not in xi or vice not in xi or captain == vice:
        raise ValueError(
            "Choose different captain and vice-captain from your starting XI."
        )


def replace_lineup(lineup, moves):
    replacements = dict(moves)
    return {
        "xi": [replacements.get(pid, pid) for pid in lineup["xi"]],
        "captain": replacements.get(lineup["captain"], lineup["captain"]),
        "vice": replacements.get(lineup["vice"], lineup["vice"]),
    }


def remap_squad_lineup(lineup, old_ids, new_ids, players):
    """Retained IDs keep their roles; only genuine same-position additions inherit."""
    removed = [pid for pid in old_ids if pid not in new_ids]
    added = [pid for pid in new_ids if pid not in old_ids]
    moves = []
    for position in (1, 2, 3, 4):
        outgoing = sorted(
            pid for pid in removed if players[pid]["element_type"] == position
        )
        incoming = sorted(
            pid for pid in added if players[pid]["element_type"] == position
        )
        moves.extend(zip(outgoing, incoming))
    return replace_lineup(lineup, moves) if lineup else {}
