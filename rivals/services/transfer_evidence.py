"""Player comparisons using the planner forecast and published match history."""

from collections import defaultdict
from math import cos, pi, sin
from statistics import mean

from rivals.services.decision_engine import number
from rivals.services.fpl_snapshot_service import read_snapshot

AXES = (
    ("minutes", "Minutes"),
    ("expected_goal_involvements", "Expected goal involvements"),
    ("total_points", "FPL points"),
    ("starts", "Starts"),
)
DUTIES = (
    ("penalties_order", "Penalties"),
    ("direct_freekicks_order", "Direct free kicks"),
    ("corners_and_indirect_freekicks_order", "Corners and indirect free kicks"),
)


def player_axes(player, peers):
    axes = []
    for key, label in AXES:
        raw = number(player.get(key), None)
        values = [
            value for p in peers if (value := number(p.get(key), None)) is not None
        ]
        percentile = None
        if values and raw is not None:
            percentile = round(
                100
                * (sum(v < raw for v in values) + 0.5 * values.count(raw))
                / len(values)
            )
        axes.append((label, raw, percentile))
    return axes


def radar_polygon(axes):
    if any(percentile is None for _, _, percentile in axes):
        return None
    return " ".join(
        f"{100 + .55*pct*sin(i*pi/2):.1f},{70 - .55*pct*cos(i*pi/2):.1f}"
        for i, (_, _, pct) in enumerate(axes)
    )


def defensive_records(fixtures):
    records = defaultdict(list)
    for f in sorted(fixtures, key=lambda f: f.get("kickoff_time") or ""):
        home, away = f.get("team_h_score"), f.get("team_a_score")
        if not f.get("finished") or home is None or away is None:
            continue
        for club, scored, conceded in (
            (f["team_h"], home, away),
            (f["team_a"], away, home),
        ):
            result = "W" if scored > conceded else "L" if scored < conceded else "D"
            records[club].append((conceded, result))
    return records


def opponent_evidence(engine, fixture, home, history, defenses):
    opponent = fixture["team_a" if home else "team_h"]
    record = defenses[opponent]
    meetings = [
        h
        for h in history
        if h.get("opponent_team") == opponent and (h.get("minutes") or 0) > 0
    ]
    conceded = [goals for goals, _ in record]
    return {
        "opponent": engine.club_names.get(opponent, "?"),
        "venue": "Home" if home else "Away",
        "fdr": fixture.get("team_h_difficulty" if home else "team_a_difficulty"),
        "recent": mean(conceded[-5:]) if conceded else None,
        "season": mean(conceded) if conceded else None,
        "sample": min(len(record), 5),
        "games": len(record),
        "form": " ".join(result for _, result in record[-5:]),
        "appearances": len(meetings),
        "minutes": sum(h["minutes"] for h in meetings),
        "returns": sum(h.get("total_points", 0) for h in meetings),
    }


def comparison(engine, outgoing, incoming):
    peers = [
        p
        for p in engine.players.values()
        if p["element_type"] == engine.players[outgoing]["element_type"]
        and not p.get("removed")
    ]
    completed = {f["id"] for f in engine.fixtures if f.get("finished") and f.get("id")}
    defenses = defensive_records(engine.fixtures)
    profiles = []
    ids = (outgoing, incoming) if incoming is not None else (outgoing,)
    for pid in ids:
        player = engine.players[pid]
        axes = player_axes(player, peers)
        publication = read_snapshot(f"element:{engine.context['season']}:{pid}") or {}
        history = sorted(
            (
                h
                for h in publication.get("history", [])
                if h.get("fixture") in completed
            ),
            key=lambda h: h.get("kickoff_time") or "",
        )
        points = {
            item["gameweek"]: item["points"] for item in engine.player_fixtures[pid]
        }
        fixtures = []
        for week in range(
            engine.weeks[0], min(39, max(engine.weeks[-1] + 1, engine.weeks[0] + 5))
        ):
            matches = [
                opponent_evidence(engine, fixture, home, history, defenses)
                for fixture, home in engine.fixture_index.get(
                    (player["team"], week), []
                )
            ]
            fixtures.append(
                {"week": week, "matches": matches, "points": points.get(week)}
            )
        profiles.append(
            {
                "id": pid,
                "name": engine.name(pid),
                "axes": axes,
                "polygon": radar_polygon(axes),
                "history": history[-5:],
                "fixtures": fixtures,
                "duties": [(label, player.get(key)) for key, label in DUTIES],
            }
        )
    return profiles
