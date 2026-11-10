"""Bound rendered rival choices while keeping full server-side validation."""

from django.db.models import Q

LIMIT = 50


def rival_choices(queryset, *, search="", selected=None):
    matches = queryset
    if search:
        matches = matches.filter(
            Q(team_name__icontains=search[:100])
            | Q(manager_name__icontains=search[:100])
        )
    teams = list(
        matches.only("id", "team_name").order_by(
            "mini_league_teams__current_rank", "pk"
        )[:LIMIT]
    )
    try:
        selected = int(selected) if selected else None
    except (ValueError, TypeError):
        selected = None
    if selected and all(team.pk != selected for team in teams):
        team = queryset.filter(pk=selected).only("id", "team_name").first()
        if team:
            teams.insert(0, team)
    return [("", "Auto: league leader")] + [(team.pk, team.team_name) for team in teams]
