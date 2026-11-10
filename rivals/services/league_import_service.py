"""Bounded HTTP batches with durable checkpoints and atomic publication."""

from django.db import transaction
from django.utils import timezone

from rivals.models import (
    LeagueImport,
    LeagueImportPage,
    MiniLeagueTeam,
    SyncState,
    Team,
)


class LeagueImportService:
    PAGES_PER_BATCH = 5
    REPAIR_ROUNDS = 3
    MAX_AGE = timezone.timedelta(hours=24)

    def __init__(self, state, api, context):
        self.state = state
        self.api = api
        self.context = context
        self.stamp = f'{context["season"]}:{context["scoring_gameweek"]}'

    def _check_owner(self):
        if not SyncState.objects.filter(
            pk=self.state.pk, generation=self.state.generation, status="running"
        ).exists():
            raise RuntimeError("League import lease superseded")

    def run_batch(self):
        with transaction.atomic():
            self._check_owner()
            run = LeagueImport.objects.filter(league_id=self.state.object_id).first()
            if run and (
                run.stamp != self.stamp
                or timezone.now() - run.created_at > self.MAX_AGE
            ):
                run.delete()
                run = None
            if run is None:
                run = LeagueImport.objects.create(
                    league_id=self.state.object_id, stamp=self.stamp
                )

        for _ in range(self.PAGES_PER_BATCH):
            if run.next_page == 0:
                break
            page = run.next_page
            data = self.api.fetch_mini_league_page(run.league.fpl_league_id, page)
            rows = self._validated_rows(data, page)
            standings = data["standings"]
            with transaction.atomic():
                self._check_owner()
                LeagueImportPage.objects.create(
                    import_run=run, number=page, standings=rows
                )
                if page == 1:
                    run.league_info = data["league"]
                run.next_page = page + 1 if standings["has_next"] else 0
                run.save(update_fields=["league_info", "next_page"])

        if run.next_page:
            return False
        if not self._reconcile(run):
            return False
        self._publish(run)
        return True

    def _reconcile(self, run):
        """Recover entries lost to unstable ordering at tied page boundaries.

        Keep the original slot count: deduplication alone would silently lose teams.
        Repair reads accumulate entries instead of replacing previously fetched ones.
        """
        pages = list(run.pages.order_by("number"))
        metadata = run.league_info.get("_rivals_pagination")
        if (
            metadata
            and len({row["entry"] for page in pages for row in page.standings})
            == metadata["expected"]
        ):
            # Publication may have failed after reconciliation already completed.
            return True
        if metadata is None:
            seen, overlaps = {}, set()
            for page in pages:
                for row in page.standings:
                    if row["entry"] in seen:
                        overlaps.add((row["rank"], row["total"]))
                    seen[row["entry"]] = row
            if not overlaps:
                return True
            boundaries = {
                page.number
                for page in pages
                if any(
                    (row["rank"], row["total"]) in overlaps for row in page.standings
                )
            }
            candidates = sorted(
                {
                    number
                    for boundary in boundaries
                    for number in (boundary - 1, boundary, boundary + 1)
                    if 1 <= number <= len(pages)
                }
            )
            metadata = {
                "expected": sum(len(page.standings) for page in pages),
                "candidates": candidates,
                "pending": candidates[:],
                "round": 0,
            }
            run.league_info["_rivals_pagination"] = metadata
            with transaction.atomic():
                self._check_owner()
                run.save(update_fields=["league_info"])
            # Yield after the original HTTP batch before starting repair requests.
            return False

        by_number = {page.number: page for page in pages}
        for _ in range(self.PAGES_PER_BATCH):
            if not metadata["pending"]:
                break
            number = metadata["pending"][0]
            data = self.api.fetch_mini_league_page(run.league.fpl_league_id, number)
            rows = self._validated_rows(data, number)
            page = by_number[number]
            latest = {row["entry"]: row for row in rows}
            merged = {row["entry"]: row for row in page.standings}
            merged.update(latest)
            page.standings = list(merged.values())
            with transaction.atomic():
                self._check_owner()
                # Update every occurrence so publication always uses the newest read.
                for stored_page in pages:
                    if stored_page is page or any(
                        row["entry"] in latest for row in stored_page.standings
                    ):
                        stored_page.standings = [
                            latest.get(row["entry"], row)
                            for row in stored_page.standings
                        ]
                        stored_page.save(update_fields=["standings"])
                metadata["pending"] = metadata["pending"][1:]
                run.save(update_fields=["league_info"])

        unique_count = len({row["entry"] for page in pages for row in page.standings})
        if unique_count == metadata["expected"]:
            return True
        if unique_count > metadata["expected"]:
            with transaction.atomic():
                self._check_owner()
                run.delete()
            raise RuntimeError(
                "League membership changed during pagination; restarting import"
            )
        if not metadata["pending"]:
            metadata["pending"] = metadata["candidates"][:]
            metadata["round"] += 1
            exhausted = metadata["round"] >= self.REPAIR_ROUNDS
            if exhausted:
                metadata["round"] = 0
            with transaction.atomic():
                self._check_owner()
                run.save(update_fields=["league_info"])
            if exhausted:
                raise RuntimeError(
                    f"Standings reconciliation found {unique_count} of "
                    f"{metadata['expected']} teams; saved pages retained for retry"
                )
        return False

    @staticmethod
    def _validated_rows(data, page):
        if not isinstance(data, dict) or not isinstance(data.get("standings"), dict):
            raise RuntimeError(f"Standings page {page} unavailable")
        standings = data["standings"]
        rows = standings.get("results")
        if (
            not isinstance(rows, list)
            or not isinstance(standings.get("has_next"), bool)
            or (not rows and standings["has_next"])
            or standings.get("page", page) != page
            or (page == 1 and not data.get("league", {}).get("name"))
        ):
            raise RuntimeError(f"Invalid standings page {page}")
        for row in rows:
            if (
                not isinstance(row, dict)
                or type(row.get("entry")) is not int
                or row["entry"] <= 0
                or not isinstance(row.get("entry_name"), str)
                or not isinstance(row.get("player_name"), str)
                or type(row.get("rank")) is not int
                or row["rank"] < 0
                or type(row.get("total")) is not int
            ):
                raise RuntimeError(f"Invalid team in standings page {page}")
        return rows

    def _publish(self, run):
        pages = list(run.pages.order_by("number").values_list("standings", flat=True))
        unique_rows = {row["entry"]: row for page in pages for row in page}

        with transaction.atomic():
            self._check_owner()
            stale = set(
                MiniLeagueTeam.objects.filter(mini_league_id=run.league_id).values_list(
                    "pk", flat=True
                )
            )
            rows = list(unique_rows.values())
            for offset in range(0, len(rows), 500):
                self._publish_page(run, rows[offset : offset + 500], stale)
            stale = list(stale)
            for offset in range(0, len(stale), 500):
                MiniLeagueTeam.objects.filter(
                    pk__in=stale[offset : offset + 500]
                ).delete()
            league = run.league
            league.name = run.league_info["name"]
            league.last_synced_at = timezone.now()
            league.save(update_fields=["name", "last_synced_at"])
            run.delete()

    def _publish_page(self, run, rows, stale):
        ids = [str(row["entry"]) for row in rows]
        teams = {t.fpl_team_id: t for t in Team.objects.filter(fpl_team_id__in=ids)}
        Team.objects.bulk_create(
            [
                Team(
                    fpl_team_id=str(row["entry"]),
                    team_name=row["entry_name"],
                    manager_name=row["player_name"],
                    current_total_points=row["total"],
                )
                for row in rows
                if str(row["entry"]) not in teams
            ]
        )
        teams = {t.fpl_team_id: t for t in Team.objects.filter(fpl_team_id__in=ids)}
        memberships = {
            m.team_id: m
            for m in MiniLeagueTeam.objects.filter(
                mini_league_id=run.league_id, team_id__in=[t.pk for t in teams.values()]
            )
        }
        changed, created = [], []
        for row in rows:
            team = teams[str(row["entry"])]
            team.team_name = row["entry_name"]
            team.manager_name = row["player_name"]
            team.current_total_points = row["total"]
            member = memberships.get(team.pk)
            if member:
                stale.discard(member.pk)
                member.previous_rank = (
                    member.current_rank
                    if member.season == self.context["season"]
                    else None
                )
                changed.append(member)
            else:
                member = MiniLeagueTeam(mini_league_id=run.league_id, team=team)
                created.append(member)
            member.season = self.context["season"]
            member.current_rank = row["rank"]
            member.total_points = row["total"]
        Team.objects.bulk_update(
            list(teams.values()),
            ["team_name", "manager_name", "current_total_points"],
            batch_size=500,
        )
        MiniLeagueTeam.objects.bulk_create(created, batch_size=500)
        MiniLeagueTeam.objects.bulk_update(
            changed,
            ["season", "previous_rank", "current_rank", "total_points"],
            batch_size=500,
        )
