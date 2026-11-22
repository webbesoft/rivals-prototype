"""Chip opportunities reconciled with the squad ledger."""

from rivals.services.xi_benchmark import best_projected_xi

CHIP_NAMES = {
    "Triple Captain": "3xc",
    "Bench Boost": "bboost",
    "Free Hit": "freehit",
    "Wildcard": "wildcard",
}
FREE_HIT_GAIN_THRESHOLD = 15


def chip_advice(engine, baseline):
    # These are transparent planning heuristics, not FPL rules or probabilities.
    def reliable(pid):
        player = engine.players[pid]
        chance = player.get("chance_of_playing_next_round")
        return (
            player.get("status", "a") == "a"
            and (chance is None or float(chance) >= 100)
            and engine.risk[pid] <= 0.45
        )

    weeks = []
    for lineup in baseline["lineups"]:
        week = lineup["gameweek"]
        bench = [pid for pid in engine.ids if pid not in lineup["xi"]]
        weeks.append(
            {
                "week": week,
                "xi": lineup["xi"],
                "points": lineup["points"],
                "blanks": sum(
                    not engine.fixture_index.get((engine.players[pid]["team"], week))
                    for pid in lineup["xi"]
                ),
                "captain": lineup["captain"],
                "captain_points": engine.projections[lineup["captain"]][week],
                "bench": bench,
                "bench_points": sum(engine.projections[pid][week] for pid in bench),
                "missing": sum(
                    engine.projections[pid][week] <= 0 for pid in lineup["xi"]
                ),
            }
        )
    current = weeks[0]["week"]
    expiry = 19 if current <= 19 else 38
    forecast_weeks = weeks
    # Each half has its own chips. A stronger GW20 opportunity cannot justify
    # saving a first-half chip past its last usable deadline in GW19.
    weeks = [w for w in weeks if w["week"] <= expiry]
    advice = []
    # Compare reliable captain options first; an injured star must not hide a
    # slightly lower, fit player's strong chip opportunity in another week.
    captains = [
        {
            **w,
            "captain": pid,
            "captain_points": engine.projections[pid][w["week"]],
            "baseline_captain_points": w["captain_points"],
        }
        for w in weeks
        for pid in w["xi"]
        if reliable(pid)
    ]
    tc = max(captains or weeks, key=lambda w: w["captain_points"])
    strong_tc = tc["captain_points"] >= 10 and reliable(tc["captain"])
    player = engine.name(tc["captain"])
    fixtures = next(
        f["opponents"]
        for f in engine.player_fixtures[tc["captain"]]
        if f["gameweek"] == tc["week"]
    )
    advice.append(
        {
            "chip": "Triple Captain",
            "status": (
                ("Consider now" if tc["week"] == current else "Plan ahead")
                if strong_tc
                else "Hold for now"
            ),
            "gameweek": tc["week"],
            "player": player,
            "projected_points": round(tc["captain_points"], 1),
            "extra_points": round(
                2 * tc["captain_points"]
                - tc.get("baseline_captain_points", tc["captain_points"]),
                1,
            ),
            "detail": (
                f"{player} projects {tc['captain_points']:.1f} points against "
                f"{', '.join(fixtures) or 'no scheduled opponent'}: "
                f"{3 * tc['captain_points']:.1f} with Triple Captain, "
                f"an extra {2 * tc['captain_points'] - tc.get('baseline_captain_points', tc['captain_points']):.1f} over baseline captaincy. "
                + (
                    "This is a strong opportunity in your current squad's forecast. "
                    if strong_tc
                    else "No fit, secure captain reaches our 10-point opportunity threshold in this window. "
                )
                + "Check minutes and team news before committing."
            ),
        }
    )
    safe_benches = [
        w
        for w in weeks
        if all(
            reliable(pid) and engine.projections[pid][w["week"]] > 0
            for pid in w["bench"]
        )
    ]
    bb = max(safe_benches or weeks, key=lambda w: w["bench_points"])
    strong_bb = bb in safe_benches and bb["bench_points"] >= 15
    advice.append(
        {
            "chip": "Bench Boost",
            "status": (
                ("Consider now" if bb["week"] == current else "Plan ahead")
                if strong_bb
                else "Hold for now"
            ),
            "gameweek": bb["week"],
            "projected_points": round(bb["bench_points"], 1),
            "detail": (
                f"Your forecast bench ({', '.join(engine.name(pid) for pid in bb['bench'])}) "
                f"totals {bb['bench_points']:.1f} points. "
                + (
                    "All four have fixtures and pass the current availability and minutes checks. "
                    if strong_bb
                    else "Look for at least 15 projected bench points with all four likely to play. "
                )
                + "This is the bench total before any automatic substitutions; the extra return may be lower if substitutes would otherwise be needed."
            ),
        }
    )
    comparisons = []
    for week in weeks:
        benchmark = best_projected_xi(
            [
                {
                    **player,
                    "expected_points": engine.projections[pid].get(week["week"], 0),
                }
                for pid, player in engine.players.items()
                if not player.get("removed", False)
            ],
            captaincy=True,
        )
        if benchmark is None:
            continue
        # Compare both sides with captaincy using the same forecast model.
        points = float(benchmark["points"])
        following = next(
            (w for w in forecast_weeks if w["week"] == week["week"] + 1), None
        )
        temporary_blank = (
            week["blanks"] >= 3 and following is not None and following["blanks"] <= 1
        )
        comparisons.append(
            {
                **week,
                "benchmark_points": points,
                "gain": points - week["points"],
                "temporary_blank": temporary_blank,
            }
        )
    qualifying = [
        w
        for w in comparisons
        if w["gain"] >= FREE_HIT_GAIN_THRESHOLD
        and (current <= 19 or w["temporary_blank"])
    ]
    opportunity = max(qualifying or comparisons, key=lambda w: w["gain"], default=None)
    strong_fh = bool(qualifying)
    advice.append(
        {
            "chip": "Free Hit",
            "status": (
                ("Review now" if opportunity["week"] == current else "Plan ahead")
                if strong_fh
                else "Hold for now"
            ),
            "gameweek": opportunity["week"] if opportunity else current,
            "projected_gain": round(opportunity["gain"], 1) if opportunity else None,
            "squad_points": round(opportunity["points"], 1) if opportunity else None,
            "benchmark_points": (
                round(opportunity["benchmark_points"], 1) if opportunity else None
            ),
            "detail": (
                (
                    f"The team-of-the-week benchmark projects {opportunity['benchmark_points']:.1f} "
                    f"points versus your XI's {opportunity['points']:.1f} in GW{opportunity['week']}, "
                    f"a {opportunity['gain']:.1f}-point gap including captaincy. "
                    if opportunity
                    else "No complete benchmark XI is available. "
                )
                + (
                    "Review a one-week rebuild when the projected gap is at least 15 points. "
                    if current <= 19
                    else "Save your second-half Free Hit for blank gameweeks: require at least three blank-fixture XI slots, recovery to at most one the following week, and a projected gap of at least 15 points. "
                )
                + "The benchmark follows formation and club limits but has no budget cap; confirm an affordable 15-player squad and compare free transfers before committing. "
                "Both teams use the planner's projections; the landing page uses FPL expected points."
            ),
        }
    )
    persistent = [
        pid
        for pid in engine.ids
        if all(
            not reliable(pid) or engine.projections[pid][w["week"]] <= 0
            for w in forecast_weeks
        )
    ]
    rebuild = len(forecast_weeks) >= 2 and len(persistent) >= 4
    advice.append(
        {
            "chip": "Wildcard",
            "status": "Review now" if rebuild else "Hold for now",
            "gameweek": current,
            "concern_count": len(persistent),
            "squad_health": [pid in persistent for pid in engine.ids],
            "detail": (
                f"{len(persistent)} of your 15 players have availability, minutes or blank-fixture concerns throughout this window. "
                + (
                    "Review a lasting rebuild if normal transfers cannot resolve these issues. "
                    if rebuild
                    else "There is no clear widespread, lasting rebuild trigger. "
                )
                + "Compare an affordable replacement squad before committing; no Wildcard points gain has been calculated."
            ),
        }
    )
    used = engine.chip_usage()
    for item in advice:
        item["expiry_gameweek"] = expiry
        item["deadlines_remaining"] = expiry - current + 1
        used_week = used.get(CHIP_NAMES[item["chip"]])
        item["available"] = used_week is None
        item["used_gameweek"] = used_week
        if used_week is not None:
            item["status"] = "Used"
            item["gameweek"] = used_week
            item["detail"] = (
                f"Used in GW{used_week}, according to your squad ledger. "
                + (
                    "A fresh chip is available from GW20."
                    if current <= 19
                    else "No further use is available in this chip window."
                )
            )
            continue
        if current == expiry:
            if item["status"] == "Hold for now":
                item["status"] = "Review before expiry"
            item["detail"] += (
                f" GW{expiry} is the final chance to use this chip set. "
                "If several chips remain, prioritise one: only one can be played this gameweek."
            )
        elif expiry - current < 4:
            item["detail"] += (
                f" Only {expiry - current + 1} deadlines remain to use this chip set, "
                f"including GW{current}; unused chips expire at the GW{expiry} deadline. "
                "Allow a separate gameweek for each remaining chip."
            )
    return advice
