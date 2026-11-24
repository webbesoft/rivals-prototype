"""Explicit deployment choice for user-facing forecasts; experiments stay fixed."""

import hashlib
import json

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db.models import Count, Max
from django.utils import timezone

from .calibrated_component_forecast import (
    CalibratedComponentForecast,
    shadow_fingerprint,
)
from .forecast_history_service import read_histories
from .player_forecast import PlayerForecast


def active_model_name():
    name = settings.FPL_PLAYER_MODEL
    if name not in (PlayerForecast.version, CalibratedComponentForecast.version):
        raise ImproperlyConfigured(
            "FPL_PLAYER_MODEL must be heuristic-v1 or decision-v12-calibrated"
        )
    return name


def active_model_identity():
    return (
        shadow_fingerprint()
        if active_model_name() == CalibratedComponentForecast.version
        else PlayerForecast.version
    )


def active_cache_stamp(season):
    """Invalidate persisted analysis caches on model changes and history expiry."""
    identity = active_model_identity()
    if active_model_name() == PlayerForecast.version:
        return identity
    from rivals.models import FplSnapshot

    now = timezone.now()
    stamp = FplSnapshot.objects.filter(
        key__startswith=f"element:{season}:", expires_at__gt=now, updated_at__lte=now
    ).aggregate(count=Count("pk"), updated=Max("updated_at"))
    return identity, stamp


def build_player_forecast(
    players, fixtures, scoring_gameweek, *, teams=(), season=None
):
    players = list(players)
    fixtures = list(fixtures)
    if active_model_name() == PlayerForecast.version:
        model = PlayerForecast(players, fixtures, scoring_gameweek)
        model.cache_identity = PlayerForecast.version
        model.display_name = "Standard points model"
        model.experimental = False
        model.method = "Form and points per game, adjusted for season minutes, current availability and each scheduled fixture."
        return model
    if not season:
        raise ImproperlyConfigured(
            "The revised player model requires a published FPL season"
        )
    model = CalibratedComponentForecast(
        players,
        fixtures,
        scoring_gameweek,
        teams=teams,
        histories=read_histories(season),
        season=season,
    )
    fields = (
        "id",
        "element_type",
        "team",
        "minutes",
        "expected_goals",
        "expected_assists",
        "status",
        "chance_of_playing_next_round",
    )
    inputs = [
        active_model_identity(),
        season,
        scoring_gameweek,
        [{key: p.get(key) for key in fields} for p in players],
        list(model.teams.values()),
        model.histories,
        [
            {key: f.get(key) for key in ("event", "team_h", "team_a", "finished")}
            for f in fixtures
        ],
    ]
    model.cache_identity = hashlib.sha256(
        json.dumps(inputs, sort_keys=True).encode()
    ).hexdigest()
    model.display_name = "Revised component model · Experimental"
    model.experimental = True
    model.method = "Expected minutes, underlying xG/xA and individual scoring components, with damped team strengths and fitted attacker calibration."
    return model
