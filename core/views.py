import datetime
from django.shortcuts import render
from django.http import HttpResponse

from rivals.services.landing_data_service import LandingDataService


def index(request):
    landing_service = LandingDataService()

    context = {
        "current_gameweek": landing_service.get_current_gameweek(),
        "best_xi": landing_service.get_best_xi(),
        "captain_picks": landing_service.get_top_captain_picks(),
        "transfer_trends": landing_service.get_transfer_trends(),
        "fixture_highlights": landing_service.get_fixture_difficulty_highlights(),
        "demo_team": landing_service.get_demo_team_data(),
    }

    return render(request, "pages/index.html", context)


def health_check(request):
    return HttpResponse("OK")


def robots_txt(request):
    sitemap_url = request.build_absolute_uri("/sitemap.xml")
    lines = [
        "User-agent: *",
        "Allow: /",
        "Disallow: /admin/",
        "Disallow: /api/",
        "",
        f"Sitemap: {sitemap_url}",
    ]
    return HttpResponse("\n".join(lines), content_type="text/plain")


def sitemap_xml(request):
    today = datetime.date.today().isoformat()
    base_url = request.build_absolute_uri("/").rstrip("/")
    urls = [
        {"loc": f"{base_url}/", "changefreq": "daily", "priority": "1.0"},
        {
            "loc": f"{base_url}/accounts/login/",
            "changefreq": "monthly",
            "priority": "0.6",
        },
        {
            "loc": f"{base_url}/accounts/signup/",
            "changefreq": "monthly",
            "priority": "0.8",
        },
    ]
    xml_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for u in urls:
        xml_lines.append("  <url>")
        xml_lines.append(f"    <loc>{u['loc']}</loc>")
        xml_lines.append(f"    <lastmod>{today}</lastmod>")
        xml_lines.append(f"    <changefreq>{u['changefreq']}</changefreq>")
        xml_lines.append(f"    <priority>{u['priority']}</priority>")
        xml_lines.append("  </url>")
    xml_lines.append("</urlset>")
    return HttpResponse("\n".join(xml_lines), content_type="application/xml")
