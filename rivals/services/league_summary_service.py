"""Published manager ranks and shared, independently refreshed standings pages."""

from django.db import transaction
from django.utils import timezone

from rivals.models import LeagueRankSnapshot, LeagueStandingsPage, MiniLeague


def save_profile_ranks(team, profile, context):
    from rivals.services.league_membership_service import account_leagues

    if str(profile.get("id")) != str(team.fpl_team_id):
        raise ValueError("FPL profile does not match team")
    season = context.get("season")
    gameweek = profile.get("current_event")
    if (
        not season
        or type(gameweek) is not int
        or not 1 <= gameweek <= 38
        or gameweek != context.get("scoring_gameweek")
    ):
        return
    rows = account_leagues(profile)
    # Only subscribed leagues need rank histories; rival profiles can contribute
    # their ranks too, without creating every league they belong to.
    leagues = MiniLeague.objects.filter(fpl_league_id__in=rows)
    with transaction.atomic():
        for league in leagues:
            row = rows[league.fpl_league_id]
            rank, previous = row.get("entry_rank"), row.get("entry_last_rank")
            points = profile.get("summary_overall_points")
            if type(rank) is not int or rank <= 0 or type(points) is not int:
                continue
            LeagueRankSnapshot.objects.update_or_create(
                team=team,
                mini_league=league,
                season=season,
                gameweek=gameweek,
                defaults={
                    "current_rank": rank,
                    "previous_rank": (
                        previous if type(previous) is int and previous > 0 else None
                    ),
                    "total_points": points,
                },
            )


def latest_rank(team, league, season):
    if not team or not season:
        return None
    return (
        LeagueRankSnapshot.objects.filter(team=team, mini_league=league, season=season)
        .order_by("-gameweek")
        .first()
    )


def save_page(league, season, number, data):
    from rivals.services.league_import_service import LeagueImportService

    rows = LeagueImportService._validated_rows(data, number)
    page, _ = LeagueStandingsPage.objects.update_or_create(
        mini_league=league,
        season=season,
        number=number,
        defaults={
            "rows": rows,
            "has_next": data["standings"]["has_next"],
            "page_size": max(1, len(rows)),
            "updated_at": timezone.now(),
        },
    )
    if number == 1:
        league.name = data["league"]["name"]
        league.last_synced_at = timezone.now()
        league.save(update_fields=["name", "last_synced_at"])
    return page


def requested_page(league, season, number):
    from rivals.services.refresh_service import enqueue_refresh

    page, _ = LeagueStandingsPage.objects.get_or_create(
        mini_league=league, season=season, number=number
    )
    if number == 1:
        enqueue_refresh("league", league.pk)
    else:
        enqueue_refresh("league_page", page.pk)
    return page


def large_league_context(user, league, context, params):
    """At most three nearby pages per visit; ties never trigger an unbounded search."""
    season = context["season"]
    rank = latest_rank(user.team, league, season)
    history = (
        list(
            LeagueRankSnapshot.objects.filter(
                team_id=user.team_id, mini_league=league, season=season
            ).order_by("gameweek")
        )
        if user.team_id and season
        else []
    )
    if not season:
        return {"rank_snapshot": rank, "rank_history": history, "waiting": False}
    first = requested_page(league, season, 1)
    size = first.page_size
    section = params.get("section", "nearby")
    estimated = (rank.current_rank - 1) // size + 1 if rank else 1
    try:
        number = max(
            1,
            min(
                1_000_000,
                int(
                    params.get(
                        "standings_page", estimated if section == "nearby" else 1
                    )
                ),
            ),
        )
    except (TypeError, ValueError):
        number = 1
    if league.rank_count:
        number = min(number, max(1, (league.rank_count + size - 1) // size))
    numbers = [number]
    if section == "nearby":
        numbers = sorted({max(1, number - 1), number, number + 1})
        if league.rank_count:
            maximum = max(1, (league.rank_count + size - 1) // size)
            numbers = [n for n in numbers if n <= maximum]
    pages = [first if n == 1 else requested_page(league, season, n) for n in numbers]
    displayed_pages = {page.number: page for page in [first, *pages]}.values()
    # A refreshed first page starts a new publication window. Retain stale pages
    # in storage, but never mix them into that window. Within it, newest wins.
    requested_pages = pages
    pages = sorted(
        [
            page
            for page in pages
            if page.updated_at
            and (first.updated_at is None or page.updated_at >= first.updated_at)
        ],
        key=lambda page: page.updated_at,
    )
    # Duplicate entries are possible across moving/tied upstream boundaries.
    by_id = {
        row["entry"]: {**row, "page_number": page.number}
        for page in pages
        for row in page.rows
    }
    rows = sorted(
        by_id.values(), key=lambda row: (row["rank"], -row["total"], row["entry"])
    )
    found = (
        any(str(row["entry"]) == str(user.team.fpl_team_id) for row in rows)
        if user.team_id
        else False
    )
    tracked_ids = set(
        user.tracked_teams.filter(active=True).values_list("team_id", flat=True)
    )
    tracked_ids.add(user.team_id)
    tracked = (
        LeagueRankSnapshot.objects.filter(
            team_id__in=tracked_ids, mini_league=league, season=season
        )
        .select_related("team")
        .order_by("team_id", "-gameweek")
    )
    tracked_latest = {}
    for item in tracked:
        tracked_latest.setdefault(item.team_id, item)
    return {
        "rank_snapshot": rank,
        "rank_history": history,
        "leaders": first.rows[:10],
        # Profiles report season totals, which cannot be compared with a
        # late-start league's points without importing additional history.
        "can_compare_points": league.start_event == 1,
        "gap_to_leader": (
            max(0, first.rows[0]["total"] - rank.total_points)
            if rank and first.rows and league.start_event == 1
            else None
        ),
        "gap_to_podium": (
            max(0, first.rows[min(2, len(first.rows) - 1)]["total"] - rank.total_points)
            if rank and first.rows and league.start_event == 1
            else None
        ),
        "visible_rows": rows,
        "tracked_ranks": list(tracked_latest.values()),
        "section": section,
        "standings_page": number,
        "previous_page": max(1, number - 1),
        "next_page": number + 1,
        "has_next": any(
            page.number >= number and page.has_next for page in requested_pages
        ),
        "found_user": found,
        "waiting": any(
            page.updated_at is None
            or (first.updated_at and page.updated_at < first.updated_at)
            for page in displayed_pages
        ),
        "pages_updated_at": min(
            (page.updated_at for page in displayed_pages if page.updated_at),
            default=None,
        ),
        "pages_stale": any(
            not page.updated_at
            or (timezone.now() - page.updated_at).total_seconds()
            > context.get("refresh_seconds", 1800)
            for page in displayed_pages
        ),
    }
