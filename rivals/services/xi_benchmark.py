"""Exact unrestricted-budget XI selection with optional captain optimisation."""

from collections import defaultdict


def best_projected_xi(players, *, captaincy=False):
    by_club = defaultdict(lambda: defaultdict(list))
    for player in {p["id"]: p for p in players}.values():
        by_club[player["team"]][player["element_type"]].append(player)
    limits = (1, 5, 5, 3)
    # shape, has_captain -> points, ids, captain
    states = {((0, 0, 0, 0), False): (0, (), None)}
    for club_id in sorted(by_club):
        club_states = {
            (shape, used, 0): result for (shape, used), result in states.items()
        }
        for position, candidates in sorted(by_club[club_id].items()):
            candidates.sort(key=lambda p: (-p["expected_points"], p["id"]))
            for player in candidates[:3]:
                updated = dict(club_states)
                for (shape, used, club_count), (
                    points,
                    ids,
                    captain,
                ) in club_states.items():
                    if (
                        club_count == 3
                        or shape[position - 1] == limits[position - 1]
                        or sum(shape) == 11
                    ):
                        continue
                    new_shape = list(shape)
                    new_shape[position - 1] += 1
                    for bonus in (
                        (False, True) if captaincy and not used else (False,)
                    ):
                        key = (tuple(new_shape), used or bonus, club_count + 1)
                        result = (
                            points + player["expected_points"] * (2 if bonus else 1),
                            ids + (player["id"],),
                            player["id"] if bonus else captain,
                        )
                        if key not in updated or result[0] > updated[key][0]:
                            updated[key] = result
                club_states = updated
        states = {}
        for (shape, used, _), result in club_states.items():
            key = (shape, used)
            if key not in states or result[0] > states[key][0]:
                states[key] = result
    legal = [
        (shape, result)
        for (shape, used), result in states.items()
        if shape[0] == 1
        and sum(shape) == 11
        and shape[1] >= 3
        and shape[2] >= 2
        and shape[3] >= 1
        and (not captaincy or used)
    ]
    if not legal:
        return None
    shape, (points, ids, captain) = max(legal, key=lambda item: item[1][0])
    return {"shape": shape, "points": points, "ids": ids, "captain": captain}
