"""Conditional chip opportunities for the unchanged squad's forecast window."""


def chip_advice(engine, baseline):
    # These are transparent planning heuristics, not FPL rules or probabilities.
    # Availability is deliberately conditional: public squads do not establish
    # which chips the manager still has, or whether one is already selected.
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
    captains = [w for w in weeks if reliable(w["captain"])]
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
            "detail": (
                f"{player} projects {tc['captain_points']:.1f} points against "
                f"{', '.join(fixtures) or 'no scheduled opponent'}: "
                f"{3 * tc['captain_points']:.1f} with Triple Captain, "
                f"an extra {tc['captain_points']:.1f} over normal captaincy. "
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
    disruption = max(weeks, key=lambda w: w["missing"])
    # Recovery after the refresh still matters for a one-week Free Hit.
    following = next(
        (w for w in forecast_weeks if w["week"] == disruption["week"] + 1), None
    )
    temporary = (
        disruption["missing"] >= 3
        and following is not None
        and following["missing"] <= 1
    )
    advice.append(
        {
            "chip": "Free Hit",
            "status": (
                ("Review now" if disruption["week"] == current else "Plan ahead")
                if temporary
                else "Hold for now"
            ),
            "gameweek": disruption["week"],
            "detail": (
                f"Your best legal XI has {disruption['missing']} zero-projection slots in GW{disruption['week']}. "
                + (
                    f"That falls to {following['missing']} the following week, suggesting a temporary squad problem. Consider a one-week rebuild if free transfers cannot cover it."
                    if temporary
                    else "A Free Hit is most useful for a temporary shortage, such as a blank gameweek, when your permanent squad looks healthy afterwards. No clear one-week recovery is established in this window."
                )
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
    for item in advice:
        item["expiry_gameweek"] = expiry
        item["deadlines_remaining"] = expiry - current + 1
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
