"""Season-scoped weekly reviews from published FPL history and effective picks."""

from collections import Counter, defaultdict

from django.db.models import F, Window
from django.db.models.functions import RowNumber
from django.utils import timezone
from rivals.services.fpl_snapshot_service import enqueue_squad

from rivals.models import (
    FplSnapshot,
    GameweekData,
    LeagueRankSnapshot,
    SeasonRankGoal,
    SquadHistory,
)
from rivals.services.decision_engine import FORMATIONS


def player_review(rows):
    scores = {
        r.player_fpl_id: (r.player_name, r.gameweek_points * r.multiplier)
        for r in rows
        if r.gameweek_points is not None
    }
    starters = [r for r in rows if r.multiplier > 0]
    review = {
        "scores": scores,
        "multipliers": {r.player_fpl_id: r.multiplier for r in rows},
        "picks_final": bool(rows) and all(r.points_final for r in rows),
        "contributors": sorted(
            (scores[r.player_fpl_id] for r in starters if r.player_fpl_id in scores),
            key=lambda item: -item[1],
        )[:3],
        "picks_at": min((r.updated_at for r in rows), default=None),
        "complete": len(scores) == 15,
    }
    captain = next(
        (r for r in starters if r.multiplier > 1 and r.gameweek_points is not None),
        None,
    )
    if captain:
        review["captain"] = scores[captain.player_fpl_id]
    if not review["complete"]:
        return review
    if captain:
        eligible = [r for r in starters if len(starters) != 15 or r.position <= 11]
        best = max(eligible, key=lambda r: r.gameweek_points)
        review["armband_gain"] = (best.gameweek_points - captain.gameweek_points) * (
            captain.multiplier - 1
        )
        review["best_captain"] = best.player_name
    if len(starters) == 15:
        review["bench_note"] = "All bench points counted."
        return review
    if len(starters) != 11:
        return review
    shape = Counter(r.element_type for r in starters)
    best_gain = 0
    for sub in (r for r in rows if r.multiplier == 0 and r.gameweek_minutes):
        for starter in starters:
            after = shape.copy()
            after[starter.element_type] -= 1
            after[sub.element_type] += 1
            if after[1] != 1 or tuple(after[k] for k in (2, 3, 4)) not in FORMATIONS:
                continue
            gain = (sub.gameweek_points - starter.gameweek_points) * starter.multiplier
            if gain > best_gain:
                best_gain = gain
                review["swap"] = (
                    sub.player_name,
                    starter.player_name,
                    gain,
                    starter.multiplier,
                )
    if not best_gain and all(r.gameweek_minutes is not None for r in rows):
        review["bench_note"] = "No legal bench swap improved the score."
    return review


def weekly_reviews(teams, season, week, league_ids):
    ids = [t.pk for t in teams]
    histories = {
        (r.team_id, r.gameweek): r
        for r in GameweekData.objects.filter(
            team_id__in=ids, season=season, gameweek__in=[week, week - 1]
        )
    }
    picks = defaultdict(list)
    for r in SquadHistory.objects.filter(
        team_id__in=ids, season=season, gameweek=week
    ).order_by("position"):
        picks[r.team_id].append(r)
    ranks = defaultdict(list)
    for r in (
        LeagueRankSnapshot.objects.filter(
            team_id__in=ids,
            season=season,
            gameweek__lte=week,
            mini_league_id__in=league_ids,
        )
        .annotate(
            latest=Window(
                RowNumber(),
                partition_by=[F("team_id"), F("mini_league_id")],
                order_by=F("gameweek").desc(),
            )
        )
        .filter(latest=1)
        .select_related("mini_league")
        .order_by("mini_league__name")
    ):
        ranks[r.team_id].append(r)
    profiles = FplSnapshot.objects.filter(
        key__in=[f"profile:{season}:{team.fpl_team_id}" for team in teams]
    ).in_bulk(field_name="key")
    reviews = []
    for team in teams:
        current = histories.get((team.pk, week))
        prior = histories.get((team.pk, week - 1))
        score = current.points if current else None
        rank = current.overall_rank if current else None
        score_at = rank_at = current.updated_at if current else None
        hits = current.event_transfers_cost if current else None
        profile = profiles.get(f"profile:{season}:{team.fpl_team_id}")
        if (
            profile
            and profile.payload.get("current_event") == week
            and (not current or profile.updated_at > current.updated_at)
        ):
            if profile.payload.get("summary_event_points") is not None:
                score, score_at = (
                    profile.payload["summary_event_points"],
                    profile.updated_at,
                )
            if profile.payload.get("summary_overall_rank"):
                rank, rank_at = (
                    profile.payload["summary_overall_rank"],
                    profile.updated_at,
                )
        movement = (
            prior.overall_rank - rank if prior and prior.overall_rank and rank else None
        )
        reviews.append(
            {
                **player_review(picks[team.pk]),
                "team": team,
                "week": week,
                "history": current,
                "score": score,
                "hits": hits,
                "score_at": score_at,
                "rank": rank,
                "rank_at": rank_at,
                "movement": movement,
                "ranks": ranks[team.pk],
                "net": score - hits if score is not None and hits is not None else None,
            }
        )
    return reviews


def explain_gap(mine, rival):
    if mine["net"] is None or rival["net"] is None:
        return {}
    swing = mine["net"] - rival["net"]
    result = {"swing": swing}
    if not (mine["complete"] and rival["complete"]):
        return result
    if any(
        sum(points for _, points in side["scores"].values())
        != side["net"] + side["history"].event_transfers_cost
        for side in (mine, rival)
    ):
        return result
    diffs = []
    for pid in mine["scores"].keys() | rival["scores"].keys():
        name, home = mine["scores"].get(pid, ("", 0))
        away_name, away = rival["scores"].get(pid, ("", 0))
        if home != away:
            diffs.append(
                (
                    name or away_name,
                    home - away,
                    mine["multipliers"].get(pid, 0),
                    rival["multipliers"].get(pid, 0),
                )
            )
    hits = rival["history"].event_transfers_cost - mine["history"].event_transfers_cost
    result.update(diffs=sorted(diffs, key=lambda d: (-abs(d[1]), d[0])), hits=hits)
    return result


def briefing(user, context, requested_week, teams, leagues):
    season, latest = context.get("season"), context.get("scoring_gameweek")
    if not season or not user.team_id:
        return {}
    goal = SeasonRankGoal.objects.filter(
        user=user, team=user.team, season=season
    ).first()
    if not latest:
        return {"season": season, "goal": goal}
    previous = min(latest - 1, context.get("latest_checked_gameweek", 0))
    week = previous if previous and requested_week == str(previous) else latest
    reviews = weekly_reviews(
        [user.team, *teams], season, week, [league.pk for league in leagues]
    )
    cutoff = timezone.now() - timezone.timedelta(seconds=120)
    for review in reviews:
        if not review["complete"] or (
            not review["picks_final"]
            and (
                week <= context.get("latest_checked_gameweek", 0)
                or review["picks_at"] < cutoff
            )
        ):
            enqueue_squad(review["team"], season, week, context=context)
    mine = reviews[0]
    for rival in reviews[1:]:
        rival["gap"] = explain_gap(mine, rival)
    trend = list(
        GameweekData.objects.filter(
            team=user.team,
            season=season,
            gameweek__lte=week,
            overall_rank__isnull=False,
        )
        .order_by("gameweek")
        .values_list("gameweek", "overall_rank")
    )
    if mine["rank"]:
        trend = [entry for entry in trend if entry[0] != week] + [(week, mine["rank"])]
    chart = {}
    if trend:
        ranks = [rank for _, rank in trend] + ([goal.rank] if goal else [])

        def y(rank):
            return round(4 + 52 * (rank - min(ranks)) / (max(ranks) - min(ranks) or 1))

        chart = {
            "line": " ".join(
                f"{4+292*(gw-1)/max(1,week-1):.1f},{y(rank)}" for gw, rank in trend
            ),
            "target_y": y(goal.rank) if goal else None,
            "values": trend,
        }
    rank = trend[-1][1] if trend else None
    return {
        "season": season,
        "week": week,
        "latest": latest,
        "previous": previous,
        "final": week <= context.get("latest_checked_gameweek", 0),
        "mine": mine,
        "rivals": reviews[1:],
        "goal": goal,
        "chart": chart,
        "rank": rank,
        "rank_week": trend[-1][0] if trend else None,
        "goal_gap": rank - goal.rank if rank and goal else None,
        "leagues": [
            (lg, next((r for r in mine["ranks"] if r.mini_league_id == lg.pk), None))
            for lg in leagues
        ],
    }
