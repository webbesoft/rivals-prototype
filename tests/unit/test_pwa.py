import struct
from pathlib import Path

from django.contrib.staticfiles import finders
from django.template.loader import render_to_string
from django.test import SimpleTestCase
from django.urls import reverse


class PWAResourcesTests(SimpleTestCase):
    def test_manifest_is_public_and_icons_exist_at_declared_sizes(self):
        response = self.client.get(reverse("core:manifest"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/manifest+json")
        manifest = response.json()
        self.assertEqual(manifest["scope"], "/")
        self.assertEqual(manifest["start_url"], "/dashboard/")
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(len(manifest["icons"]), 3)
        for icon in manifest["icons"]:
            data = Path(finders.find(icon["src"].removeprefix("/static/"))).read_bytes()
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
            width, height = struct.unpack("!II", data[16:24])
            self.assertEqual(icon["sizes"], f"{width}x{height}")

    def test_worker_is_public_root_scoped_javascript_and_revalidated(self):
        self.assertEqual(reverse("core:service_worker"), "/service-worker.js")
        response = self.client.get(reverse("core:service_worker"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/javascript")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertContains(response, 'const OFFLINE_URL = "/offline/";')

    def test_offline_page_is_self_contained_and_never_sets_a_session(self):
        response = self.client.get(reverse("core:offline"))
        self.assertContains(response, "You're offline")
        self.assertEqual(response["X-Robots-Tag"], "noindex")
        self.assertNotIn("sessionid", response.cookies)
        self.assertNotIn(b"/static/", response.content)
        self.assertNotIn(b"<script", response.content)

    def test_base_layout_links_install_resources(self):
        html = render_to_string("cotton/layouts/base.html", {"landing": True})
        self.assertIn('rel="manifest" href="/manifest.webmanifest"', html)
        self.assertIn('rel="apple-touch-icon"', html)
        self.assertIn("/static/js/pwa.js", html)
