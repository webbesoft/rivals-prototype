from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from rivals.models import FplSnapshot, Team, TrackedTeam


@pytest.mark.django_db
@pytest.mark.parametrize(
    "my_points,rival_points,shown_points,gap,label",
    [
        (610, 690, 690, "−80", "pts behind"),
        (704, 690, 690, "+14", "pts ahead"),
        (690, 690, 690, "0", "level on points"),
        (0, 12, 12, "−12", "pts behind"),
        (12, 0, 0, "+12", "pts ahead"),
        (None, 690, 690, "−90", "pts behind"),
        (610, None, 620, "−10", "pts behind"),
        (None, None, 620, "−20", "pts behind"),
    ],
)
def test_watchlist_uses_published_entry_totals_for_points_and_gap(
    my_points, rival_points, shown_points, gap, label
):
    mine = Team.objects.create(
        fpl_team_id="101",
        team_name="My XI",
        current_total_points=600,
        current_season="2026/27",
    )
    rival = Team.objects.create(
        fpl_team_id="202",
        team_name="Rival XI",
        current_total_points=620,
        current_season="2026/27",
    )
    user = User.objects.create_user(username="dashboard", team=mine)
    TrackedTeam.objects.create(user=user, team=rival)
    for team, points in [(mine, my_points), (rival, rival_points)]:
        FplSnapshot.objects.create(
            key=f"profile:2025/26:{team.fpl_team_id}",
            payload={"summary_overall_points": 2000},
            updated_at=timezone.now(),
            expires_at=timezone.now() + timezone.timedelta(hours=1),
        )
        if points is None:
            continue
        FplSnapshot.objects.create(
            key=f"profile:2026/27:{team.fpl_team_id}",
            payload={"summary_overall_points": points},
            updated_at=timezone.now(),
            expires_at=timezone.now() + timezone.timedelta(hours=1),
        )
    client = Client()
    client.force_login(user)
    with (
        patch("rivals.views.dashboard_views.enqueue_user"),
        patch(
            "rivals.views.dashboard_views.GameweekContextService.cached",
            return_value={"season": "2026/27"},
        ),
        patch(
            "rivals.services.projection_service.ProjectionService.project_team_finish",
            return_value=None,
        ),
        patch(
            "rivals.services.fpl_api_service.FplApiService.make_request",
            side_effect=AssertionError("Dashboard must not call FPL"),
        ),
    ):
        response = client.get(reverse("rivals:dashboard"))
    assert response.status_code == 200
    html = response.content.decode()
    assert f"{shown_points} pts" in html
    assert f"<strong>{gap}</strong><span>{label}</span>" in html
