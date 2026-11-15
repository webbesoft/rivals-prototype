"""Select a projection benchmark XI with legal formations and club limits."""

from collections import defaultdict


def best_projected_xi(players):
    by_club = defaultdict(lambda: defaultdict(list))
    players = {p["id"]: p for p in players}
    for player in players.values():
        by_club[player["team"]][player["element_type"]].append(player)
    # Dynamic programming keeps the best score for each positional count.
    # The temporary club count enforces the cap without greedy selection.
    limits = (1, 5, 5, 3)
    states = {(0, 0, 0, 0): (0, ())}
    for club_id in sorted(by_club):
        club_states = {(shape, 0): result for shape, result in states.items()}
        for position, candidates in sorted(by_club[club_id].items()):
            # At most three players from any club can feature, so lower
            # scorers at the same club and position cannot improve the XI.
            candidates.sort(key=lambda p: (-p["expected_points"], p["id"]))
            for player in candidates[:3]:
                updated = dict(club_states)
                for (shape, club_count), (points, ids) in club_states.items():
                    if (
                        club_count == 3
                        or shape[position - 1] == limits[position - 1]
                        or sum(shape) == 11
                    ):
                        continue
                    new_shape = list(shape)
                    new_shape[position - 1] += 1
                    key = (tuple(new_shape), club_count + 1)
                    result = (
                        points + player["expected_points"],
                        ids + (player["id"],),
                    )
                    if key not in updated or result[0] > updated[key][0]:
                        updated[key] = result
                club_states = updated
        states = {}
        for (shape, _), result in club_states.items():
            if shape not in states or result[0] > states[shape][0]:
                states[shape] = result

    legal = [
        (shape, result)
        for shape, result in states.items()
        if shape[0] == 1
        and sum(shape) == 11
        and shape[1] >= 3
        and shape[2] >= 2
        and shape[3] >= 1
    ]
    if not legal:
        return None
    shape, (points, ids) = max(legal, key=lambda item: item[1][0])
    return {"shape": shape, "points": points, "ids": ids}
