import re

from django.utils import timezone

from rivals.models import SquadHistory
from rivals.services.fpl_api_service import FplApiService
from rivals.services.gameweek_context_service import GameweekContextService
from rivals.services.team_sync_service import TeamSyncService


class SquadHistoryService:
    def __init__(self, team, api=None):
        self.team = team
        self.api = api or FplApiService()
        self.api.TIMEOUT = 4
        self.context = GameweekContextService.cached()

    def selection(self, season=None, gameweek=None):
        seasons = set(
            self.team.squad_histories.exclude(season__isnull=True).values_list(
                "season", flat=True
            )
        )
        seasons.update(
            self.team.gameweek_data.exclude(season__isnull=True).values_list(
                "season", flat=True
            )
        )
        default = (
            self.context["season"]
            or self.team.current_season
            or (max(seasons) if seasons else None)
        )
        if default:
            seasons.add(default)
        season = season or default
        if season and (
            not re.fullmatch(r"\d{4}/\d{2}", season) or season not in seasons
        ):
            raise ValueError("Choose an available season.")
        weeks = set(
            self.team.squad_histories.filter(season=season).values_list(
                "gameweek", flat=True
            )
        )
        weeks.update(
            self.team.gameweek_data.filter(season=season).values_list(
                "gameweek", flat=True
            )
        )
        if season == self.context["season"] and self.context["scoring_gameweek"]:
            # Public picks can be requested individually without a full history import.
            weeks.update(
                range(
                    self.team.started_event or 1, self.context["scoring_gameweek"] + 1
                )
            )
        weeks = sorted(week for week in weeks if 1 <= week <= 38)
        if gameweek not in (None, ""):
            try:
                gameweek = int(gameweek)
            except (TypeError, ValueError):
                raise ValueError("Choose an available gameweek.")
            if gameweek not in weeks:
                raise ValueError("Choose an available gameweek.")
        else:
            gameweek = weeks[-1] if weeks else None
        return {
            "season": season,
            "seasons": sorted(seasons, reverse=True),
            "gameweek": gameweek,
            "available_gameweeks": weeks,
        }

    def load(self, season, gameweek):
        if gameweek is None:
            return [], None
        rows = list(
            self.team.squad_histories.filter(season=season, gameweek=gameweek).order_by(
                "position"
            )
        )
        current_season = season == self.context["season"]
        recent = rows and (timezone.now() - rows[0].updated_at).total_seconds() < 120
        complete = len(rows) == 15
        if complete and (
            not current_season
            or (
                all(p.gameweek_points is not None for p in rows)
                and (all(p.points_final for p in rows) or recent)
            )
        ):
            return rows, None
        if not current_season or self.context["stale"]:
            if complete:
                return rows, (
                    "This lineup may be out of date." if current_season else None
                )
            return [], "This lineup is currently unavailable."
        bootstrap = self.api.fetch_bootstrap_static()
        if (
            not bootstrap
            or GameweekContextService.derive(bootstrap.get("events", []))["season"]
            != season
        ):
            return (
                rows if complete else []
            ), "We couldn't update this lineup. Please try again."
        service = TeamSyncService(self.team)
        service.fpl_api_service = self.api
        picks = service.get_gameweek_picks(gameweek)
        if not picks:
            return (
                rows if complete else []
            ), "We couldn't load this lineup. Please try again."
        live = self.api.fetch_gameweek_live(gameweek, season)
        live_points = {
            p["id"]: p.get("stats", {}).get("total_points")
            for p in (live or {}).get("elements", [])
        }
        scores_available = bool(
            live is not None
            and all(
                live_points.get(pick["element"]) is not None
                for pick in picks.get("picks", [])
            )
        )
        if complete and not scores_available:
            return rows, "Player scores may be out of date."
        final = scores_available and any(
            event["id"] == gameweek
            and event.get("finished")
            and event.get("data_checked")
            for event in bootstrap["events"]
        )
        success = service.sync_squad_for_gameweek(
            gameweek,
            picks=picks,
            elements={p["id"]: p for p in bootstrap.get("elements", [])},
            points=live_points,
            points_final=bool(final),
            refresh=True,
            season=season,
        )
        if not success:
            return (
                rows if complete else []
            ), "We couldn't load the complete lineup. Please try again."
        rows = list(
            self.team.squad_histories.filter(season=season, gameweek=gameweek).order_by(
                "position"
            )
        )
        return rows, (
            None if scores_available else "Player scores are currently unavailable."
        )
