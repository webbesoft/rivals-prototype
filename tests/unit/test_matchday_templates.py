from types import SimpleNamespace

from django.contrib.auth.models import AnonymousUser
from django.core.paginator import Paginator
from django.template.loader import render_to_string
from django.test import RequestFactory, override_settings


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_comparison_renders_fractional_points_gaps():
    request = RequestFactory().get("/")
    request.user = AnonymousUser()
    my_team = SimpleNamespace(id=1, team_name="My XI", current_total_points=100)
    rival_team = SimpleNamespace(id=2, team_name="Rival XI", current_total_points=90)
    html = render_to_string(
        "pages/teams/compare.html",
        {
            "my_team": my_team,
            "rival_team": rival_team,
            "comparison": {
                "position_comparisons": [
                    {
                        "position": "MID",
                        "advantage": "mine",
                        "my_team_expected": 15.7,
                        "rival_team_expected": 13.4,
                    }
                ]
            },
        },
        request=request,
    )
    assert "+10 pts ahead" in html
    assert "+2.3" in html


def test_pagination_preserves_filters_and_marks_current_page():
    request = RequestFactory().get("/?season=2026&page=4")
    paginator = Paginator(range(90), 10)
    html = render_to_string(
        "cotton/components/pagination.html",
        {"page_obj": paginator.page(4)},
        request=request,
    )
    assert 'aria-current="page" aria-label="Page 4"' in html
    assert 'href="?season=2026&amp;page=3" rel="prev"' in html
    assert 'href="?season=2026&amp;page=5" rel="next"' in html
