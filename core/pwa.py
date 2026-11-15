"""Public PWA resources; never include session or live FPL data here."""

from django.http import JsonResponse
from django.shortcuts import render
from django.templatetags.static import static
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe


@require_safe
def manifest(request):
    return JsonResponse(
        {
            "id": "/",
            "name": "RivalSpy",
            "short_name": "RivalSpy",
            "description": "FPL rival tracking and mini-league intelligence.",
            "lang": "en",
            "start_url": "/dashboard/",
            "scope": "/",
            "display": "standalone",
            "background_color": "#0f4f2f",
            "theme_color": "#0f4f2f",
            "icons": [
                {
                    "src": static(f"images/pwa/icon-{size}.png"),
                    "sizes": f"{size}x{size}",
                    "type": "image/png",
                    "purpose": "any",
                }
                for size in (192, 512)
            ]
            + [
                {
                    "src": static("images/pwa/icon-maskable-512.png"),
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "maskable",
                }
            ],
        },
        content_type="application/manifest+json",
    )


@require_safe
@never_cache
def service_worker(request):
    return render(
        request,
        "pwa/service-worker.js",
        {"offline_url": reverse("core:offline")},
        content_type="text/javascript",
    )


@require_safe
@never_cache
def offline(request):
    response = render(request, "pwa/offline.html")
    response["X-Robots-Tag"] = "noindex"
    return response
