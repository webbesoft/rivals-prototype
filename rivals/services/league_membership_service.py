"""Reconcile FPL account memberships without deleting league history."""

from django.db import transaction

from rivals.models import MiniLeague, UserMiniLeague


def account_leagues(profile):
    """Reject incomplete snapshots before making any membership changes."""
    leagues = profile.get("leagues") if isinstance(profile, dict) else None
    classic = leagues.get("classic") if isinstance(leagues, dict) else None
    if not isinstance(classic, list):
        raise ValueError("Complete FPL classic league list required")
    result = {}
    for row in classic:
        if (
            not isinstance(row, dict)
            or type(row.get("id")) is not int
            or row["id"] <= 0
            or not isinstance(row.get("name"), str)
            or not row["name"]
            or row.get("league_type") not in ("s", "x")
        ):
            raise ValueError("Invalid FPL league snapshot")
        if row["league_type"] == "x" and row.get("scoring", "c") == "c":
            result[str(row["id"])] = row
    return result


def reconcile_memberships(user, profile, *, retire_legacy=True):
    """Refresh account imports, including old unclassified memberships.

    Legacy memberships are treated as account imports on the first validated
    profile refresh. Explicit manual memberships are never retired here.
    """
    current = account_leagues(profile)
    if str(profile.get("id")) != str(user.team.fpl_team_id):
        raise ValueError("FPL profile does not match the linked team")
    with transaction.atomic():
        # A profile refresh must not attach memberships after an account switch.
        if not type(user).objects.filter(pk=user.pk, team_id=user.team_id).exists():
            return
        for fpl_id, row in current.items():
            metadata = {
                key: row[key]
                for key in (
                    "has_cup",
                    "start_event",
                    "entry_can_leave",
                    "entry_can_admin",
                    "entry_can_invite",
                )
                if key in row
            }
            if "admin_entry" in row:
                metadata["admin_entry_id"] = (
                    str(row["admin_entry"]) if row["admin_entry"] else None
                )
            league, _ = MiniLeague.objects.update_or_create(
                fpl_league_id=fpl_id,
                defaults={
                    "name": row["name"],
                    "league_type": row["league_type"],
                    "scoring_type": row.get("scoring", "c"),
                    "rank_count": row.get("rank_count"),
                    **metadata,
                },
            )
            membership, created = UserMiniLeague.objects.get_or_create(
                user=user,
                mini_league=league,
                defaults={"source": "fpl", "active": True},
            )
            if not created and membership.source != "manual":
                UserMiniLeague.objects.filter(pk=membership.pk).update(
                    source="fpl", active=True
                )
        sources = ["fpl", "legacy"] if retire_legacy else ["fpl"]
        user.customminileagues.filter(source__in=sources).exclude(
            mini_league__fpl_league_id__in=current
        ).update(active=False, source="fpl")
