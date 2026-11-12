from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from rivals.models import Team, TrackedTeam


@pytest.fixture
def team_pages(client, db):
    team = Team.objects.create(fpl_team_id="48291", team_name="Page United")
    user = get_user_model().objects.create_user(
        username="pages", email="pages@example.com", password="test123456", team=team
    )
    client.force_login(user)
    with (
        patch("rivals.views.team_views.enqueue_team"),
        patch("requests.get", side_effect=AssertionError("Unexpected HTTP request")),
        patch(
            "rivals.views.team_views.ProjectionService.project_team_finish",
            return_value=None,
        ) as projection,
    ):
        yield team, projection


@pytest.mark.django_db
@pytest.mark.parametrize(
    "section,expected,excluded",
    [
        (
            "show",
            "Lineup & squad history",
            [
                "Mini-League Standings",
                "Recent Gameweek Ledger",
                "Transfer Ledger",
                "Career Archive",
            ],
        ),
        (
            "performance",
            "Recent Gameweek Ledger",
            ["squad-history-panel", "Transfer Ledger", "Career Archive"],
        ),
        (
            "transfers",
            "Transfer Ledger",
            ["squad-history-panel", "Recent Gameweek Ledger", "Career Archive"],
        ),
        (
            "history",
            "Career Archive",
            ["squad-history-panel", "Recent Gameweek Ledger", "Transfer Ledger"],
        ),
    ],
)
def test_team_sections_have_separate_content_and_navigation(
    client, team_pages, section, expected, excluded
):
    team, projection = team_pages
    response = client.get(reverse(f"rivals:teams.{section}", args=[team.pk]))
    assert response.status_code == 200
    html = response.content.decode()
    assert expected in html
    for heading in excluded:
        assert heading not in html
    for page in ("show", "performance", "transfers", "history"):
        assert reverse(f"rivals:teams.{page}", args=[team.pk]) in html
    assert html.count('aria-current="page"') >= 1
    if section == "performance":
        projection.assert_called_once()
    else:
        projection.assert_not_called()
    if section == "show":
        assert html.index("squad-history-panel") < html.index('id="squad-briefing"')


@pytest.mark.django_db
@pytest.mark.parametrize("section", ["show", "performance", "transfers", "history"])
def test_team_sections_require_login_and_existing_team(client, team_pages, section):
    url = reverse(f"rivals:teams.{section}", args=[999999])
    assert client.get(url).status_code == 404
    client.logout()
    assert client.get(url).status_code == 302


@pytest.mark.django_db
def test_teams_index_opens_own_squad(client, team_pages):
    team, _ = team_pages
    response = client.get(reverse("rivals:teams.index"))
    assert response.status_code == 302
    assert response.url == reverse("rivals:teams.show", args=[team.pk])


@pytest.mark.django_db
def test_rival_sync_requires_tracking_and_denial_stays_in_the_site(client, team_pages):
    own_team, _ = team_pages
    user = get_user_model().objects.get(team=own_team)
    rival = Team.objects.create(fpl_team_id="48292", team_name="Rival United")
    sync_url = reverse("rivals:teams.sync", args=[rival.pk])
    show_url = reverse("rivals:teams.show", args=[rival.pk])
    html = client.get(show_url).content.decode()
    assert sync_url not in html
    assert "+ Track Rival" in html

    with patch("rivals.views.team_views.enqueue_team") as enqueue:
        response = client.post(sync_url, follow=True)
        enqueue.assert_called_once_with(rival.pk)  # Viewing the redirect only.
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/html")
    assert "Track this rival first to request updates." in response.content.decode()

    tracking = TrackedTeam.objects.create(user=user, team=rival)
    assert sync_url in client.get(show_url).content.decode()
    tracking.active = False
    tracking.save(update_fields=["active"])
    assert sync_url not in client.get(show_url).content.decode()
    assert (
        reverse("rivals:teams.sync", args=[own_team.pk])
        in client.get(reverse("rivals:teams.show", args=[own_team.pk])).content.decode()
    )
