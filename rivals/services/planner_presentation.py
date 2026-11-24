"""Shared forecast presentation for the planner and deadline emails."""


def planner_pitch_weeks(report):
    weeks = []
    for lineup in report["baseline"]["lineups"]:
        points = round(lineup["points"], 1)
        benchmark = report.get("benchmarks", {}).get(lineup["gameweek"])
        comparison = None
        if benchmark and benchmark["points"] > 0:
            benchmark_points = round(benchmark["points"], 1)
            comparison = {
                "points": benchmark_points,
                "gap": round(max(0, benchmark_points - points), 1),
            }
        players = []
        for player in report["players"]:
            fixture = next(
                f for f in player["schedule"] if f["gameweek"] == lineup["gameweek"]
            )
            players.append(
                {
                    **player,
                    "player_name": player["name"],
                    "fixture": fixture,
                    "is_captain": player["id"] == lineup["captain"]
                    and (lineup.get("selection") == "planned" or fixture["points"] > 0),
                    "is_vice_captain": player["id"] == lineup["vice"]
                    and (lineup.get("selection") == "planned" or fixture["points"] > 0),
                }
            )
        starters = [p for p in players if p["id"] in lineup["xi"]]
        rows = [
            {
                "label": label,
                "position": kind,
                "players": [p for p in starters if p["element_type"] == kind],
            }
            for kind, label in (
                (1, "Goalkeeper"),
                (2, "Defenders"),
                (3, "Midfielders"),
                (4, "Forwards"),
            )
        ]
        weeks.append(
            {
                "gameweek": lineup["gameweek"],
                "selection": lineup.get("selection", "optimised"),
                "points": points,
                "benchmark": comparison,
                "rows": rows,
                "formation": "-".join(str(len(row["players"])) for row in rows[1:]),
                "bench": sorted(
                    (p for p in players if p["id"] not in lineup["xi"]),
                    key=lambda p: (
                        p["element_type"] != 1,
                        -p["fixture"]["points"],
                        p["id"],
                    ),
                ),
            }
        )
    return weeks
