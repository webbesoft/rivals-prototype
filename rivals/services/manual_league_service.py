"""Validate an upstream classic league before any local records are written."""

import requests

from .fpl_api_service import FplApiService


def validated_league(raw_id):
    raw_id = raw_id.strip()
    if (
        len(raw_id) > 10
        or not raw_id.isascii()
        or not raw_id.isdecimal()
        or not 0 < int(raw_id) <= 2147483647
    ):
        raise ValueError("Enter a valid positive mini-league ID.")
    fpl_id = str(int(raw_id))
    try:
        data = FplApiService().fetch_mini_league_page(fpl_id, 1)
    except requests.RequestException:
        raise ValueError("FPL is unavailable. Please try again shortly.") from None
    league = data.get("league") if isinstance(data, dict) else None
    standings = data.get("standings") if isinstance(data, dict) else None
    if (
        not isinstance(league, dict)
        or str(league.get("id")) != fpl_id
        or not isinstance(league.get("name"), str)
        or not league["name"].strip()
        or len(league["name"]) > 255
        or league.get("scoring", "c") != "c"
        or not isinstance(standings, dict)
        or not isinstance(standings.get("results"), list)
    ):
        raise ValueError(
            "Could not validate that classic mini-league. Please check the ID and try again."
        )
    return fpl_id, league["name"].strip()
