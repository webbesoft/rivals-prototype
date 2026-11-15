from django.urls import path

from core import pwa, views

app_name = "core"

urlpatterns = [
    path("manifest.webmanifest", pwa.manifest, name="manifest"),
    path("service-worker.js", pwa.service_worker, name="service_worker"),
    path("offline/", pwa.offline, name="offline"),
    path("", views.index, name="index"),
    path("health", views.health_check, name="health_check"),
    path("robots.txt", views.robots_txt, name="robots_txt"),
    path("sitemap.xml", views.sitemap_xml, name="sitemap_xml"),
]
