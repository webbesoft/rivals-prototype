"""Account-scoped published FPL queries. No HTTP, ingestion or scheduling writes.

Browser orchestration may schedule work separately; MCP uses these reads alone.
"""

import hashlib
import json
import re
from datetime import timedelta

from django.conf import settings
from django.core import signing
from django.conf import settings
from django.db.models import F, Max, OuterRef, Q, Subquery
from django.urls import reverse
from django.utils import timezone

from rivals.models import (
    FplSnapshot,
    LeagueRankSnapshot,
    LeagueStandingsPage,
    MiniLeagueTeam,
    SyncState,
    Team,
)
from rivals.services.access_service import accessible_leagues
from rivals.services.gameweek_context_service import GameweekContextService


class ReadUnavailable(ValueError):
    pass


def snapshot(key):
    """Return the publication, including expired data, without requesting refresh."""
    return FplSnapshot.objects.filter(key=key).first()


def stamp(name, updated_at=None, expires_at=None, seconds=1800):
    expiry = expires_at or (
        updated_at + timedelta(seconds=seconds) if updated_at else None
    )
    return {
        "source": name,
        "updated_at": updated_at,
        "expires_at": expiry,
        "status": (
            "missing"
            if not updated_at
            else "fresh" if expiry > timezone.now() else "stale"
        ),
    }


class PublishedReads:
    def __init__(self, user):
        if not user.is_active or not user.is_verified:
            raise ReadUnavailable("A verified active Rivals account is required.")
        if settings.PASSWORDLESS_AUTH_ENABLED:
            from accounts.onboarding import has_fpl_setup

            if not has_fpl_setup(user):
                raise ReadUnavailable("Connect your FPL team in Rivals before continuing.")
        self.user = user
        self.context = GameweekContextService.cached()
        self.season = self.context["season"]
        self.leagues = accessible_leagues(user)

    def result(
        self,
        data,
        *,
        sources=(),
        coverage="complete",
        season=None,
        gameweek=None,
        assumptions=(),
        next_cursor=None,
    ):
        return {
            "schema_version": "1",
            "season": season or self.season,
            "gameweek": gameweek,
            "assembled_at": timezone.now(),
            "data": data,
            "sources": list(sources),
            "coverage": coverage,
            "assumptions": list(assumptions),
            "next_cursor": next_cursor,
            "rivals_url": settings.SITE_URL.rstrip("/") + reverse("rivals:dashboard"),
        }

    def team_ref(self, team):
        return {
            "reference": f"entry:{team.fpl_team_id}",
            "name": team.team_name,
            "manager": team.manager_name,
        }

    def team(self, reference="me"):
        if reference == "me":
            if not self.user.team_id:
                raise ReadUnavailable("Team unavailable. Link your team in Rivals.")
            return self.user.team
        if not re.fullmatch(r"entry:[1-9]\d{0,11}", reference):
            raise ReadUnavailable("Team unavailable.")
        entry = reference.split(":")[1]
        team = Team.objects.filter(fpl_team_id=entry, active=True).first()
        if team and (
            team.pk == self.user.team_id
            or self.user.tracked_teams.filter(team=team, active=True).exists()
            or self.season
            and MiniLeagueTeam.objects.filter(
                team=team, mini_league__in=self.leagues, season=self.season
            ).exists()
        ):
            return team
        # Summary standings may contain entries that have not been imported.
        # Bound the scan; never allocate a team or request another standings page.
        if team and self.season:
            leader_publication = LeagueStandingsPage.objects.filter(
                mini_league_id=OuterRef("mini_league_id"), season=self.season, number=1
            ).values("updated_at")[:1]
            for rows in (
                LeagueStandingsPage.objects.filter(
                    mini_league__in=self.leagues,
                    season=self.season,
                    updated_at__isnull=False,
                )
                .annotate(leader_updated=Subquery(leader_publication))
                .filter(
                    Q(leader_updated__isnull=True)
                    | Q(updated_at__gte=F("leader_updated"))
                )
                .order_by("mini_league_id", "number")
                .values_list("rows", flat=True)[:100]
            ):
                if any(str(row.get("entry")) == entry for row in rows[:100]):
                    return team
        raise ReadUnavailable("Team unavailable.")

    def league(self, reference):
        if not re.fullmatch(r"league:[1-9]\d{0,11}", reference):
            raise ReadUnavailable("League unavailable.")
        league = self.leagues.filter(fpl_league_id=reference.split(":")[1]).first()
        if not league:
            raise ReadUnavailable("League unavailable.")
        return league

    def selected_season(self, season):
        season = season or self.season
        if not season or not re.fullmatch(r"\d{4}/\d{2}", season):
            raise ReadUnavailable("Season unavailable.")
        if int(season[5:]) != (int(season[:4]) + 1) % 100:
            raise ReadUnavailable("Season unavailable.")
        return season

    def page(self, queryset, key, filters, cursor, limit, publication=None):
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ReadUnavailable("Choose between 1 and 50 results.")
        if not isinstance(queryset, list):
            publication = queryset.aggregate(last=Max("updated_at"))["last"]
        access = {
            "leagues": list(
                self.leagues.order_by("pk").values_list("pk", "updated_at")
            ),
            "memberships": list(
                self.user.customminileagues.order_by("pk").values_list(
                    "pk", "active", "updated_at"
                )
            ),
            "tracking": list(
                self.user.tracked_teams.order_by("pk").values_list(
                    "pk", "active", "updated_at"
                )
            ),
            "team": self.user.team_id,
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                [self.user.pk, key, filters, str(publication), str(access), limit],
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()
        offset = 0
        if cursor:
            try:
                saved = signing.loads(cursor, salt="mcp-page", max_age=900)
                if (
                    saved["fingerprint"] != fingerprint
                    or type(saved["offset"]) is not int
                ):
                    raise ValueError
                offset = saved["offset"]
                if not 0 <= offset <= 10000:
                    raise ValueError
            except (signing.BadSignature, ValueError, KeyError, TypeError):
                raise ReadUnavailable(
                    "This page has changed. Restart without a cursor."
                ) from None
        rows = list(queryset[offset : offset + limit + 1])
        following = (
            signing.dumps(
                {"fingerprint": fingerprint, "offset": offset + limit}, salt="mcp-page"
            )
            if len(rows) > limit and offset + limit <= 10000
            else None
        )
        return rows[:limit], following, publication

    def get_my_context(self):
        state = SyncState.objects.filter(key="context").first()
        return self.result(
            {
                "team": self.team_ref(self.user.team) if self.user.team_id else None,
                "scoring_gameweek": self.context["scoring_gameweek"],
                "decision_gameweek": self.context["decision_gameweek"],
                "next_deadline": self.context["next_deadline"],
            },
            sources=[
                stamp("context", state.last_success_at if state else None, seconds=600)
            ],
            coverage="complete" if self.season else "unavailable",
        )

    def list_my_leagues(self, query="", cursor=None, limit=20):
        qs = self.leagues.filter(name__icontains=query).order_by("pk")
        rows, following, updated = self.page(qs, "leagues", [query], cursor, limit)
        return self.result(
            [
                {
                    "reference": f"league:{league.fpl_league_id}",
                    "name": league.name,
                    "scoring_type": league.scoring_type,
                    "summary_only": league.uses_summary,
                    "own_rank": self._rank(league),
                    "updated_at": league.last_synced_at,
                }
                for league in rows
            ],
            next_cursor=following,
            sources=[stamp("league list", updated)],
        )

    def _rank(self, league):
        if not self.season:
            return None
        rank = (
            LeagueRankSnapshot.objects.filter(
                mini_league=league, team_id=self.user.team_id, season=self.season
            )
            .order_by("-gameweek")
            .first()
        )
        if rank:
            return {
                "rank": rank.current_rank,
                "previous_rank": rank.previous_rank,
                "points": rank.total_points,
                "gameweek": rank.gameweek,
                "updated_at": rank.updated_at,
            }
        row = MiniLeagueTeam.objects.filter(
            mini_league=league, team_id=self.user.team_id, season=self.season
        ).first()
        return (
            {
                "rank": row.current_rank,
                "points": row.total_points,
                "gameweek": None,
                "updated_at": row.updated_at,
            }
            if row
            else None
        )

    def get_league_standings(self, league, section="leaders", cursor=None, limit=20):
        league = self.league(league)
        if not self.season:
            return self.result({"own_rank": None, "rows": []}, coverage="unavailable")
        if section not in ("leaders", "near_me"):
            raise ReadUnavailable("Choose leaders or near_me.")
        own = self._rank(league)
        minimum = max(1, own["rank"] - 10) if section == "near_me" and own else 1
        if league.is_head_to_head_scoring():
            return self.result(
                {"own_rank": own, "rows": []},
                coverage="unavailable",
                assumptions=["Head-to-head match standings are not supported."],
            )
        if league.uses_summary:
            number = (minimum - 1) // 50 + 1
            first = LeagueStandingsPage.objects.filter(
                mini_league=league, season=self.season, number=1
            ).first()
            pages = list(
                LeagueStandingsPage.objects.filter(
                    mini_league=league,
                    season=self.season,
                    number__gte=number,
                    number__lt=number + 3,
                    updated_at__isnull=False,
                ).order_by("number")
            )
            # Match browser publication windows; don't mix obsolete pages with a refreshed leader page.
            pages = [
                p
                for p in pages
                if not first or not first.updated_at or p.updated_at >= first.updated_at
            ]
            by_entry = {}
            for page in sorted(pages, key=lambda p: p.updated_at):
                for row in page.rows[:100]:
                    if row.get("rank", 0) >= minimum:
                        by_entry[row["entry"]] = row
            published = sorted(
                by_entry.values(), key=lambda r: (r.get("rank", 0), r["entry"])
            )
            rows, following, _ = self.page(
                published,
                "summary standings",
                [league.pk, section],
                cursor,
                limit,
                publication=str([(p.number, p.updated_at) for p in pages]),
            )
            own_row = (
                by_entry.get(int(self.user.team.fpl_team_id))
                if self.user.team_id
                else None
            )
            rows = [
                {
                    "reference": f"entry:{r['entry']}",
                    "name": r.get("entry_name"),
                    "manager": r.get("player_name"),
                    "rank": r.get("rank"),
                    "previous_rank": r.get("last_rank"),
                    "points": r.get("total"),
                    "gap_to_me": r["total"] - own_row["total"] if own_row else None,
                }
                for r in rows
            ]
            return self.result(
                {
                    "own_rank": own,
                    "rows": rows,
                    "published_pages": [p.number for p in pages],
                    "requested_page": number,
                    "has_unpublished_more": bool(pages and pages[-1].has_next),
                },
                next_cursor=following,
                coverage="partial" if pages else "unavailable",
                sources=[
                    stamp(f"standings page {p.number}", p.updated_at) for p in pages
                ]
                or [stamp("standings pages")],
                assumptions=[
                    "Only published pages are available; totals from separate publications may differ."
                ],
            )
        own_standing = MiniLeagueTeam.objects.filter(
            mini_league=league, season=self.season, team_id=self.user.team_id
        ).first()
        qs = (
            MiniLeagueTeam.objects.filter(
                mini_league=league, season=self.season, current_rank__gte=minimum
            )
            .select_related("team")
            .order_by("current_rank", "pk")
        )
        rows, following, updated = self.page(
            qs, "standings", [league.pk, section], cursor, limit
        )
        return self.result(
            {
                "own_rank": own,
                "rows": [
                    {
                        **self.team_ref(row.team),
                        "rank": row.current_rank,
                        "previous_rank": row.previous_rank,
                        "points": row.total_points,
                        "gap_to_me": (
                            row.total_points - own_standing.total_points
                            if own_standing
                            else None
                        ),
                    }
                    for row in rows
                ],
            },
            sources=[stamp("standings", league.last_synced_at)],
            next_cursor=following,
            coverage="complete" if league.last_synced_at else "unavailable",
            assumptions=["Standings totals are published snapshots, not live scores."],
        )

    def list_my_rivals(self, query="", cursor=None, limit=20):
        qs = (
            Team.objects.filter(
                trackedteam__user=self.user, trackedteam__active=True, active=True
            )
            .filter(Q(team_name__icontains=query) | Q(manager_name__icontains=query))
            .order_by("pk")
        )
        rows, following, _ = self.page(qs, "rivals", [query], cursor, limit)
        return self.result([self.team_ref(row) for row in rows], next_cursor=following)

    def get_team_summary(self, team="me"):
        team = self.team(team)
        current = bool(self.season and team.current_season == self.season)
        profile = (
            snapshot(f"profile:{self.season}:{team.fpl_team_id}") if current else None
        )
        event = profile.payload.get("current_event") if profile else None
        bank = profile.payload.get("last_deadline_bank") if profile else None
        value = profile.payload.get("last_deadline_value") if profile else None
        bank_row = (
            team.gameweek_data.filter(season=self.season, bank__isnull=False)
            .order_by("-gameweek")
            .first()
            if current
            else None
        )
        if bank is None and bank_row:
            bank = bank_row.bank
        return self.result(
            {
                **self.team_ref(team),
                "total_points": (
                    team.current_total_points
                    if current and team.last_synced_at
                    else None
                ),
                "overall_rank": (
                    team.current_overall_rank
                    if current and team.last_synced_at
                    else None
                ),
                "gameweek_points": (
                    profile.payload.get("summary_event_points") if event else None
                ),
                "bank": {
                    "value": bank / 10 if bank is not None else None,
                    "unit": "GBP_millions",
                    "estimated": True,
                },
                "team_value": {
                    "value": value / 10 if value is not None else None,
                    "unit": "GBP_millions",
                },
            },
            gameweek=event,
            sources=[
                stamp(
                    "team profile",
                    (
                        profile.updated_at
                        if profile
                        else team.last_synced_at if current else None
                    ),
                    expires_at=profile.expires_at if profile else None,
                ),
                stamp("bank history", bank_row.updated_at if bank_row else None),
            ],
            coverage="complete" if current and team.last_synced_at else "unavailable",
            assumptions=[
                "Public balances and squad data can lag private pre-deadline changes."
            ],
        )

    def squad_rows(self, team, season, gameweek=None):
        qs = team.squad_histories.filter(season=season)
        weeks = list(
            qs.order_by("gameweek").values_list("gameweek", flat=True).distinct()
        )
        week = gameweek if gameweek is not None else (weeks[-1] if weeks else None)
        return list(qs.filter(gameweek=week).order_by("position")), week, weeks

    def get_team_squad(self, team="me", season=None, gameweek=None):
        team = self.team(team)
        season = self.selected_season(season)
        rows, week, weeks = self.squad_rows(team, season, gameweek)
        complete = len(rows) == 15 and len({r.player_fpl_id for r in rows}) == 15
        return self.result(
            {
                "team": self.team_ref(team),
                "available_gameweeks": weeks,
                "players": [
                    {
                        "reference": f"player:{season}:{r.player_fpl_id}",
                        "name": r.player_name,
                        "club": r.club_name,
                        "position": r.position,
                        "position_type": r.element_type,
                        "captain": r.is_captain,
                        "vice_captain": r.is_vice_captain,
                        "multiplier": r.multiplier,
                        "price": {"value": r.player_cost / 10, "unit": "GBP_millions"},
                        "points": r.gameweek_points,
                        "points_final": r.points_final,
                    }
                    for r in rows
                ],
            },
            season=season,
            gameweek=week,
            sources=[
                stamp(
                    "published squad",
                    min((r.updated_at for r in rows), default=None),
                    seconds=120,
                )
            ],
            coverage="complete" if complete else "partial" if rows else "unavailable",
            assumptions=[
                "This is the published gameweek squad, not a private upcoming lineup."
            ],
        )

    def history(self, kind, team, season, start, end, cursor, limit):
        team = self.team(team)
        season = self.selected_season(season)
        if start > end:
            raise ReadUnavailable("Start gameweek must precede end gameweek.")
        related = team.gameweek_data if kind == "history" else team.transfers
        qs = related.filter(
            season=season, gameweek__gte=start, gameweek__lte=end
        ).order_by("-gameweek", "-pk")
        rows, following, updated = self.page(
            qs, kind, [team.pk, season, start, end], cursor, limit
        )
        if kind == "history":
            data = [
                {
                    "gameweek": r.gameweek,
                    "points": r.points,
                    "total_points": r.total_points,
                    "overall_rank": r.rank,
                    "transfer_count": r.event_transfers,
                    "transfer_cost_points": r.event_transfers_cost,
                    "chip": r.chip_name,
                }
                for r in rows
            ]
        else:
            data = [
                {
                    "gameweek": r.gameweek,
                    "time": r.transfer_time,
                    "player_in": r.player_in_name,
                    "player_out": r.player_out_name,
                    "player_in_reference": (
                        f"player:{season}:{r.player_in_fpl_id}"
                        if r.player_in_fpl_id
                        else None
                    ),
                    "player_out_reference": (
                        f"player:{season}:{r.player_out_fpl_id}"
                        if r.player_out_fpl_id
                        else None
                    ),
                    "buy_price": {
                        "value": (
                            r.player_in_cost / 10
                            if r.player_in_cost is not None
                            else None
                        ),
                        "unit": "GBP_millions",
                    },
                    "sell_price": {
                        "value": (
                            r.player_out_cost / 10
                            if r.player_out_cost is not None
                            else None
                        ),
                        "unit": "GBP_millions",
                    },
                    # FPL's transfer feed does not attribute gameweek hits to individual moves.
                    "point_cost": None,
                }
                for r in rows
            ]
        state = SyncState.objects.filter(
            key=f"{'history' if kind == 'history' else 'transfers'}:{team.pk}"
        ).first()
        return self.result(
            {"team": self.team_ref(team), "rows": data},
            season=season,
            next_cursor=following,
            coverage="partial" if rows else "unavailable",
            sources=[stamp(kind, state.last_success_at if state else updated)],
            assumptions=[
                "Only recorded data are included; missing records do not establish zero transfers or points. Read gameweek history for aggregate transfer hits."
            ],
        )

    def get_team_history(
        self, team="me", season=None, start=1, end=38, cursor=None, limit=20
    ):
        return self.history("history", team, season, start, end, cursor, limit)

    def get_team_transfers(
        self, team="me", season=None, start=1, end=38, cursor=None, limit=20
    ):
        return self.history("transfers", team, season, start, end, cursor, limit)

    def compare_teams(self, first="me", second="", league=None, gameweek=None):
        a, b = self.team(first), self.team(second)
        season = self.selected_season(None)
        common = set(
            a.squad_histories.filter(season=season).values_list("gameweek", flat=True)
        ) & set(
            b.squad_histories.filter(season=season).values_list("gameweek", flat=True)
        )
        week = gameweek if gameweek is not None else max(common, default=None)
        left = self.get_team_squad(first, season, week)
        right = self.get_team_squad(second, season, week)
        # Never silently default each team to a different week.
        if week is None or any(r["coverage"] != "complete" for r in (left, right)):
            return self.result(
                {"first": self.team_ref(a), "second": self.team_ref(b)},
                coverage="unavailable",
                gameweek=week,
                assumptions=[
                    "Two complete squads from the same gameweek are required."
                ],
            )
        l = {p["reference"] for p in left["data"]["players"]}
        r = {p["reference"] for p in right["data"]["players"]}
        gap = None
        if league:
            league = self.league(league)
            standings = list(
                MiniLeagueTeam.objects.filter(
                    mini_league=league, season=season, team__in=[a, b]
                )
            )
            if len(standings) == 2:
                points = {s.team_id: s.total_points for s in standings}
                gap = {
                    "second_minus_first": points[b.pk] - points[a.pk],
                    "updated_at": league.last_synced_at,
                    "gameweek": None,
                }
        return self.result(
            {
                "first": left["data"],
                "second": right["data"],
                "shared_players": sorted(l & r),
                "first_only": sorted(l - r),
                "second_only": sorted(r - l),
                "league_gap": gap,
            },
            gameweek=week,
            sources=left["sources"] + right["sources"],
            assumptions=[
                "League totals are a separate publication; future rival choices are unknown."
            ],
        )

    def get_upcoming_fixtures(self, team=None, clubs=None, players=None, horizon=3):
        if sum(x is not None for x in (team, clubs, players)) != 1:
            raise ReadUnavailable("Choose one team, a club list, or a player list.")
        bootstrap, fixtures = snapshot("bootstrap"), snapshot("fixtures")
        if not bootstrap or not fixtures:
            return self.result(
                [],
                coverage="unavailable",
                sources=[
                    stamp("bootstrap", bootstrap.updated_at if bootstrap else None),
                    stamp("fixtures", fixtures.updated_at if fixtures else None),
                ],
            )
        context = GameweekContextService.derive(bootstrap.payload.get("events", []))
        if context["season"] != self.season or not context["decision_gameweek"]:
            return self.result([], coverage="unavailable")
        elements = {p["id"]: p for p in bootstrap.payload.get("elements", [])}
        club_names = {c["id"]: c["name"] for c in bootstrap.payload.get("teams", [])}
        squad_source = []
        if team is not None:
            squad = self.get_team_squad(team)
            if squad["coverage"] != "complete":
                return self.result([], coverage="unavailable", sources=squad["sources"])
            players = [p["reference"] for p in squad["data"]["players"]]
            squad_source = squad["sources"]
        selected_players = []
        if players is not None:
            for reference in players:
                prefix = f"player:{self.season}:"
                if (
                    not reference.startswith(prefix)
                    or not reference[len(prefix) :].isdigit()
                ):
                    raise ReadUnavailable("Choose players from the current season.")
                player = elements.get(int(reference[len(prefix) :]))
                if not player:
                    raise ReadUnavailable("Player unavailable.")
                selected_players.append(player)
            ids = {p["team"] for p in selected_players}
        else:
            ids = set()
            for reference in clubs or []:
                if (
                    not re.fullmatch(r"club:[1-9]\d{0,3}", reference)
                    or int(reference.split(":")[1]) not in club_names
                ):
                    raise ReadUnavailable(
                        "Club unavailable. Use club references from fixture results."
                    )
                ids.add(int(reference.split(":")[1]))
        start = context["decision_gameweek"]
        weeks = range(start, min(38, start + horizon - 1) + 1)
        data = []
        for club in sorted(ids):
            matches = [
                f
                for f in fixtures.payload
                if club in (f.get("team_h"), f.get("team_a")) and not f.get("finished")
            ]

            def fixture_row(f):
                home = f["team_h"] == club
                return {
                    "opponent": club_names.get(f["team_a"] if home else f["team_h"]),
                    "home": home,
                    "kickoff_utc": f.get("kickoff_time"),
                    "difficulty": f.get(
                        "team_h_difficulty" if home else "team_a_difficulty"
                    ),
                }

            data.append(
                {
                    "club": f"club:{club}",
                    "name": club_names.get(club),
                    "players": [
                        {
                            "reference": f"player:{self.season}:{p['id']}",
                            "name": p.get("web_name"),
                            "status": p.get("status"),
                            "availability_percent": p.get(
                                "chance_of_playing_next_round"
                            ),
                            "news": str(p.get("news", ""))[:500],
                        }
                        for p in selected_players
                        if p["team"] == club
                    ],
                    "gameweeks": [
                        {
                            "gameweek": w,
                            "fixtures": [
                                fixture_row(f) for f in matches if f.get("event") == w
                            ],
                        }
                        for w in weeks
                    ],
                    "unscheduled": [
                        fixture_row(f) for f in matches if f.get("event") is None
                    ],
                }
            )
        return self.result(
            data,
            gameweek=start,
            sources=squad_source
            + [
                stamp("bootstrap", bootstrap.updated_at, bootstrap.expires_at),
                stamp("fixtures", fixtures.updated_at, fixtures.expires_at),
            ],
            assumptions=[
                "Empty fixture weeks are blanks in this publication. Unscheduled matches are listed separately; availability is FPL's report."
            ],
        )
