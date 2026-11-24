"""Verified, deterministic seasonal awards. Evaluation never performs HTTP."""

from collections import defaultdict

from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from rivals.models import (
    AchievementAward,
    AchievementEvent,
    AchievementSeason,
    GameweekData,
    LeagueAchievementResult,
    SquadHistory,
    Team,
)
from .access_service import accessible_leagues

CATALOGUE = {
    "centurion": {
        "name": "Centurion",
        "symbol": "100",
        "tiers": (100, 125, 150),
        "rule": "Score 100, 125 or 150 points in a finished gameweek, before transfer hits. Chips count.",
        "unit": "points",
    },
    "green_machine": {
        "name": "Green Machine",
        "symbol": "↑",
        "tiers": (3, 5, 10),
        "rule": "Improve your overall rank for 3, 5 or 10 consecutive gameweeks. An unchanged rank ends the streak.",
        "unit": "weeks",
    },
    "captain_fantastic": {
        "name": "Captain Fantastic",
        "symbol": "C",
        "tiers": (10, 15, 20),
        "rule": "Your effective captain scores 10, 15 or 20 points before multiplication. Inherited vice-captaincy counts.",
        "unit": "points",
    },
    "on_a_roll": {
        "name": "On a Roll",
        "symbol": "↗",
        "tiers": (3, 5, 10),
        "rule": "Beat the official gameweek average for 3, 5 or 10 consecutive gameweeks. Matching it ends the streak.",
        "unit": "weeks",
    },
    "top_of_table": {
        "name": "Top of the Table",
        "symbol": "1",
        "tiers": (1,),
        "rule": "Finish a gameweek first in a private classic league with at least two entrants. Tied first counts.",
        "unit": "league",
    },
    "league_champion": {
        "name": "League Champion",
        "symbol": "★",
        "tiers": (1,),
        "rule": "Finish the season first in a private classic league with at least two entrants. Tied first counts.",
        "unit": "league",
    },
}
STREAKS = ("green_machine", "on_a_roll")


def publish_events(season, events):
    """Called inside context publication's lease-guarded transaction."""
    now = timezone.now()
    event_ids = [e["id"] for e in events if type(e.get("id")) is int]
    if not season or not event_ids:
        return
    for event in events:
        week = event.get("id")
        if type(week) is not int or not 1 <= week <= 38:
            continue
        row, _ = AchievementEvent.objects.get_or_create(season=season, gameweek=week)
        final = bool(event.get("finished") and event.get("data_checked"))
        row.finalised_at = (row.finalised_at or now) if final else None
        row.finished = bool(event.get("finished"))
        row.data_checked = bool(event.get("data_checked"))
        average = event.get("average_entry_score")
        row.average_score = average if type(average) is int else None
        # A partial/bootstrap fixture must never designate an early season end.
        row.is_last = week == 38 and week == max(event_ids)
        row.save()


def record_league_results(
    league,
    season,
    gameweek,
    rows,
    *,
    entrant_count,
    fetched_at,
    count_is_exact=True,
    leaders_complete=False,
):
    """Record only post-finalisation official ranks, including losing corrections."""
    event = AchievementEvent.objects.filter(
        season=season,
        gameweek=gameweek,
        finished=True,
        data_checked=True,
        finalised_at__isnull=False,
    ).first()
    if not event or not fetched_at or fetched_at < event.finalised_at:
        return
    if type(entrant_count) is not int or entrant_count < 1:
        return
    from django.contrib.auth import get_user_model
    from rivals.models import TrackedTeam

    interested = set(
        get_user_model()
        .objects.filter(is_active=True)
        .values_list("team_id", flat=True)
    )
    interested.update(
        TrackedTeam.objects.filter(active=True, user__is_active=True).values_list(
            "team_id", flat=True
        )
    )
    interested.update(
        LeagueAchievementResult.objects.filter(
            league=league,
            season=season,
            gameweek=gameweek,
        ).values_list("team_id", flat=True)
    )
    teams = dict(
        Team.objects.filter(
            fpl_team_id__in=[str(r["entry"]) for r in rows]
        ).values_list("fpl_team_id", "pk")
    )
    eligible = (
        league.league_type == "x" and league.scoring_type == "c" and entrant_count >= 2
    )
    for row in rows:
        team_id = teams.get(str(row["entry"]))
        rank = row.get("rank")
        if eligible and rank == 1 and team_id is None and "entry_name" in row:
            # Summary leagues only need their leaders, never a full team import.
            leader, _ = Team.objects.get_or_create(
                fpl_team_id=str(row["entry"]),
                defaults={
                    "team_name": row["entry_name"],
                    "manager_name": row.get("player_name", ""),
                    "current_season": season,
                },
            )
            team_id = leader.pk
        if team_id is None or type(rank) is not int or rank < 1:
            continue
        if team_id not in interested and not (eligible and rank == 1):
            continue
        LeagueAchievementResult.objects.update_or_create(
            team_id=team_id,
            league=league,
            season=season,
            gameweek=gameweek,
            defaults={
                "rank": rank,
                "entrant_count": entrant_count,
                "entrant_count_is_exact": count_is_exact,
                "league_name": league.name,
                "eligible": eligible,
                "verified_at": fetched_at,
            },
        )
    if leaders_complete:
        # A complete leader section can disprove a former winner even when
        # they left the league or moved off the first standings page.
        leaders = [str(r["entry"]) for r in rows if r.get("rank") == 1]
        LeagueAchievementResult.objects.filter(
            league=league,
            season=season,
            gameweek=gameweek,
            rank=1,
        ).exclude(team__fpl_team_id__in=leaders).delete()


def _streak(values, tiers):
    """None is a coverage gap; never bridge it or invent a current streak."""
    count = longest = 0
    complete = True
    earned = {}
    for week, value in values:
        if value is None:
            complete = False
            count = None
            continue
        if count is None:
            # Even a later known result cannot certify the streak across a gap.
            if not value:
                count = 0
            continue
        count = count + 1 if value else 0
        longest = max(longest, count)
        for tier in tiers:
            if count >= tier and tier not in earned:
                earned[tier] = week
    return earned, {"current": count, "longest": longest, "complete": complete}


@transaction.atomic
def evaluate_team(team, season, *, publication_guard=None):
    if publication_guard:
        publication_guard()
    events = {e.gameweek: e for e in AchievementEvent.objects.filter(season=season)}
    final = {
        w: e
        for w, e in events.items()
        if e.finalised_at and e.finished and e.data_checked
    }
    last = max(final, default=0)
    state, _ = AchievementSeason.objects.get_or_create(
        team=team,
        season=season,
        defaults={"baseline_gameweek": last},
    )
    history = {
        r.gameweek: r for r in GameweekData.objects.filter(team=team, season=season)
    }
    squads = defaultdict(list)
    for row in SquadHistory.objects.filter(team=team, season=season):
        squads[row.gameweek].append(row)
    expected = {}
    decidable = defaultdict(set)

    def award(code, tier, week, evidence, league_id=None):
        scope = str(league_id) if league_id else ""
        expected[(code, tier, week, scope)] = (evidence, league_id)

    greens, averages = [], []
    start = team.started_event or 1
    missing_squads = []
    for week in range(start, last + 1):
        event, row = final.get(week), history.get(week)
        ready = bool(event and row and row.updated_at >= event.finalised_at)
        if not ready:
            greens.append((week, None))
            averages.append((week, None))
            continue
        evidence = {
            "points": row.points,
            "hits": row.event_transfers_cost,
            "chip": row.chip_name or "",
            "summary": f"{row.points} points before hits",
        }
        decidable["centurion"].add(week)
        for tier in CATALOGUE["centurion"]["tiers"]:
            if row.points >= tier:
                award("centurion", tier, week, evidence)
        previous, prior_event = history.get(week - 1), final.get(week - 1)
        rank_ready = bool(
            row.overall_rank
            and previous
            and previous.overall_rank
            and prior_event
            and previous.updated_at >= prior_event.finalised_at
        )
        greens.append(
            (
                week,
                (
                    False
                    if week == start
                    else (
                        row.overall_rank < previous.overall_rank if rank_ready else None
                    )
                ),
            )
        )
        averages.append(
            (
                week,
                (
                    row.points > event.average_score
                    if event.average_score is not None
                    else None
                ),
            )
        )
        picks = squads[week]
        settled = len(picks) == 15 and all(
            p.points_final
            and p.gameweek_points is not None
            and p.gameweek_minutes is not None
            and p.updated_at >= event.finalised_at
            for p in picks
        )
        if not settled:
            missing_squads.append(week)
            continue
        captains = [p for p in picks if p.multiplier in (2, 3)]
        if len(captains) > 1:
            missing_squads.append(week)
            continue
        decidable["captain_fantastic"].add(week)
        if captains:
            captain = captains[0]
            for tier in CATALOGUE["captain_fantastic"]["tiers"]:
                if captain.gameweek_points >= tier:
                    award(
                        "captain_fantastic",
                        tier,
                        week,
                        {
                            "player": captain.player_name,
                            "player_id": captain.player_fpl_id,
                            "points": captain.gameweek_points,
                            "multiplier": captain.multiplier,
                            "contribution": captain.gameweek_points
                            * captain.multiplier,
                            "chip": row.chip_name or "",
                            "summary": f"{captain.player_name}: {captain.gameweek_points} points → {captain.gameweek_points * captain.multiplier} as captain",
                        },
                    )

    progress = {}
    complete_codes = set()
    for code, values in (("green_machine", greens), ("on_a_roll", averages)):
        earned, progress[code] = _streak(values, CATALOGUE[code]["tiers"])
        if progress[code]["complete"]:
            complete_codes.add(code)
        else:
            earned = {}  # Wait for complete coverage before issuing streaks.
        for tier, week in earned.items():
            data = {
                "weeks": tier,
                "start_gameweek": week - tier + 1,
                "summary": f"{tier} consecutive gameweeks, GW{week - tier + 1}–{week}",
            }
            if code == "green_machine":
                data.update(
                    previous_rank=history[week - tier].overall_rank,
                    overall_rank=history[week].overall_rank,
                )
            else:
                data["results"] = [
                    {
                        "gameweek": w,
                        "points": history[w].points,
                        "average": final[w].average_score,
                    }
                    for w in range(week - tier + 1, week + 1)
                ]
            award(code, tier, week, data)

    league_rows = list(
        LeagueAchievementResult.objects.filter(team=team, season=season).order_by(
            "gameweek", "pk"
        )
    )
    # All retained league evidence is authoritative, including later losing ranks.
    complete_codes.update(("top_of_table", "league_champion"))
    seen_leagues = set()
    for result in league_rows:
        event = final.get(result.gameweek)
        if (
            not event
            or result.verified_at < event.finalised_at
            or not result.eligible
            or result.rank != 1
        ):
            continue
        count_label = "" if result.entrant_count_is_exact else "at least "
        data = {
            "league_name": result.league_name,
            "rank": result.rank,
            "entrant_count": result.entrant_count,
            "summary": f"First in {result.league_name} · {count_label}{result.entrant_count} entrants",
        }
        if result.league_id not in seen_leagues:
            award("top_of_table", 1, result.gameweek, data, result.league_id)
            seen_leagues.add(result.league_id)
        if event.is_last:
            award("league_champion", 1, result.gameweek, data, result.league_id)

    existing = {
        (a.code, a.tier, a.gameweek, a.scope): a
        for a in AchievementAward.objects.filter(team=team, season=season)
    }
    for key, (evidence, league_id) in expected.items():
        code, tier, week, scope = key
        defaults = {
            "evidence": evidence,
            "league_id": league_id,
            "active": True,
            "rule_version": 1,
        }
        if key not in existing:
            defaults["announce"] = week > state.baseline_gameweek
        previous = existing.get(key)
        if previous is None or any(
            getattr(previous, field) != value for field, value in defaults.items()
        ):
            AchievementAward.objects.update_or_create(
                team=team,
                season=season,
                code=code,
                tier=tier,
                gameweek=week,
                scope=scope,
                defaults=defaults,
            )
    withdrawn = []
    for key, row in existing.items():
        unfinal = row.gameweek in events and row.gameweek not in final
        if row.code in STREAKS:
            values = dict(greens if row.code == "green_machine" else averages)
            first = row.evidence.get("start_gameweek", row.gameweek - row.tier + 1)
            baseline = first - 1 if row.code == "green_machine" else first
            unfinal = unfinal or any(
                w in events and w not in final
                for w in range(baseline, row.gameweek + 1)
            )
            unfinal = unfinal or any(
                values.get(w) is False for w in range(first, row.gameweek + 1)
            )
        if (
            row.active
            and key not in expected
            and (
                row.code in complete_codes
                or row.gameweek in decidable[row.code]
                or unfinal
            )
        ):
            withdrawn.append(row.pk)
    AchievementAward.objects.filter(pk__in=withdrawn).update(
        active=False, updated_at=timezone.now()
    )
    state.progress = progress
    state.evaluated_at = timezone.now()
    state.save(update_fields=["progress", "evaluated_at"])
    return {
        "season": season,
        "evaluated": len(expected),
        "withdrawn": len(withdrawn),
        "missing_squads": missing_squads,
        "progress": progress,
        "baseline_gameweek": state.baseline_gameweek,
        "final_gameweek": last,
    }


def visible_awards(user, team, season):
    return (
        AchievementAward.objects.filter(team=team, season=season, active=True)
        .filter(Q(league__isnull=True) | Q(league__in=accessible_leagues(user)))
        .select_related("league")
    )


def award_details(award):
    return {
        "id": award.pk,
        "tier": award.tier,
        "gameweek": award.gameweek,
        "name": CATALOGUE[award.code]["name"],
        "evidence": award.evidence,
        "url": (
            reverse("rivals:minileagues.show", args=[award.league_id])
            if award.league_id
            else (
                reverse("rivals:teams.show", args=[award.team_id])
                + f"?season={award.season}&gameweek={award.gameweek}#squad-history-panel"
            )
        ),
    }


def cabinet(user, team, season):
    awards = list(visible_awards(user, team, season).order_by("-gameweek", "-tier"))
    state = AchievementSeason.objects.filter(team=team, season=season).first()
    progress = state.progress if state else {}
    cards = []
    for code, definition in CATALOGUE.items():
        earned = [a for a in awards if a.code == code]
        highest = max((a.tier for a in earned), default=0)
        # One evidence row per occasion; lower tiers never inflate repeat counts.
        occasions = {}
        for a in earned:
            occasions.setdefault((a.gameweek, a.scope), award_details(a))
        streak = progress.get(code, {})
        best = max((a.evidence.get("points", 0) for a in earned), default=0)
        cards.append(
            {
                **definition,
                "code": code,
                "earned": bool(earned),
                "highest": highest,
                "count": len(occasions),
                "best": best,
                "tiers": [
                    {"value": t, "earned": t <= highest} for t in definition["tiers"]
                ],
                "occurrences": list(occasions.values()),
                "streak": streak,
                "next_tier": next(
                    (t for t in definition["tiers"] if t > highest), None
                ),
            }
        )
    return {
        "achievement_cards": cards,
        "achievement_evaluated_at": state.evaluated_at if state else None,
        "achievement_season": season,
        "achievement_earned_count": sum(c["earned"] for c in cards),
    }


def dashboard_achievements(user, season):
    if not user.team_id or not season:
        return {}
    data = cabinet(user, user.team, season)
    latest = (
        AchievementEvent.objects.filter(season=season, finalised_at__isnull=False)
        .order_by("-gameweek")
        .first()
    )
    announcements = []
    if latest:
        awards = (
            visible_awards(user, user.team, season)
            .filter(
                announce=True,
                gameweek=latest.gameweek,
            )
            .exclude(achievementdismissal__user=user)
            .order_by("code", "scope", "-tier")
        )
        seen = set()
        for a in awards:
            key = (a.code, a.scope)
            if key not in seen:
                announcements.append(award_details(a))
                seen.add(key)
    milestones = [
        c
        for c in data["achievement_cards"]
        if c["code"] in STREAKS
        and c["next_tier"]
        and c["streak"].get("current") is not None
        and c["streak"].get("complete")
    ]
    milestones.sort(key=lambda c: (c["next_tier"] - c["streak"]["current"], c["code"]))
    return {
        "achievement_announcements": announcements,
        "achievement_milestone": milestones[0] if milestones else None,
        "achievement_earned_count": data["achievement_earned_count"],
    }


def league_champions(league, season):
    return (
        AchievementAward.objects.filter(
            league=league,
            season=season,
            code="league_champion",
            active=True,
        )
        .select_related("team")
        .order_by("team__team_name")
    )


def queue_achievement_followups(kind, object_id, season):
    """Invoked only after a successful publication commits."""
    from .refresh_service import enqueue_refresh

    if kind in ("profile", "history"):
        team_ids = [object_id]
    elif kind == "squad":
        from rivals.models import SquadRefresh

        team_ids = SquadRefresh.objects.filter(pk=object_id).values_list(
            "team_id", flat=True
        )
    elif kind in ("league", "league_page"):
        if kind == "league_page":
            from rivals.models import LeagueStandingsPage

            object_id = LeagueStandingsPage.objects.get(pk=object_id).mini_league_id
        team_ids = set(
            LeagueAchievementResult.objects.filter(
                league_id=object_id, season=season
            ).values_list("team_id", flat=True)
        )
        team_ids.update(
            AchievementAward.objects.filter(
                league_id=object_id, season=season, active=True
            ).values_list("team_id", flat=True)
        )
    else:
        return  # Context changes are picked up by the bounded minute dispatcher.
    for team_id in team_ids:
        enqueue_refresh("achievements", team_id, force=True)


def queue_missing_squads(team, season):
    """At most two historical lineups per team per dispatcher pass."""
    from .fpl_snapshot_service import enqueue_squad

    state = AchievementSeason.objects.filter(team=team, season=season).first()
    if not state or not state.evaluated_at:
        return
    events = list(
        AchievementEvent.objects.filter(
            season=season,
            finalised_at__isnull=False,
            gameweek__gte=team.started_event or 1,
        ).order_by("gameweek")
    )
    rows = defaultdict(list)
    for p in SquadHistory.objects.filter(team=team, season=season):
        rows[p.gameweek].append(p)
    pending = 0
    for e in events:
        picks = rows[e.gameweek]
        complete = len(picks) == 15 and all(
            p.points_final
            and p.gameweek_points is not None
            and p.gameweek_minutes is not None
            and p.updated_at >= e.finalised_at
            for p in picks
        )
        # Recheck settled data daily so late authoritative corrections propagate.
        due = complete and min(
            p.updated_at for p in picks
        ) < timezone.now() - timezone.timedelta(days=1)
        if not complete or due:
            if enqueue_squad(team, season, e.gameweek, force=due):
                pending += 1
            if pending >= 2:
                break
