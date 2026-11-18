import gzip
from types import SimpleNamespace

import pytest
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.test import override_settings
from django.urls import path


def document(request):
    return HttpResponse("transfer outlook " * 500)


urlpatterns = [path("", document)]


@override_settings(ROOT_URLCONF=__name__, ALLOWED_HOSTS=["testserver"])
@pytest.mark.parametrize("accept_encoding", ["gzip", "identity"])
def test_document_compression_negotiates_with_browser(client, accept_encoding):
    response = client.get("/", HTTP_ACCEPT_ENCODING=accept_encoding)
    assert response.status_code == 200
    original = ("transfer outlook " * 500).encode()
    if accept_encoding == "gzip":
        assert response["Content-Encoding"] == "gzip"
        assert gzip.decompress(response.content) == original
        assert len(response.content) < len(original)
        assert "Accept-Encoding" in response["Vary"]
    else:
        assert response.get("Content-Encoding") is None
        assert response.content == original


def test_squad_outlook_reserves_card_space_before_request():
    html = render_to_string(
        "pages/teams/squad.html",
        {
            "user": SimpleNamespace(team_id=1),
            "team": SimpleNamespace(id=1),
        },
    )
    assert 'aria-label="Loading squad lineup"' in html
    assert "outlook-slot" in html
    assert "skeleton--outlook" in html
    assert 'aria-label="Loading transfer outlook"' in html


def test_planner_reserves_space_through_both_loading_stages():
    html = render_to_string("pages/decisions.html")
    assert "skeleton--planner" in html
    preview = render_to_string("partials/decision_preview.html")
    assert "skeleton--results" in preview


def test_ai_polling_keeps_card_skeleton_while_queued():
    html = render_to_string("partials/ai_briefing.html", {"ai_state": "queued"})
    assert "skeleton--ai" in html
    assert "Preparing your priorities" not in html
