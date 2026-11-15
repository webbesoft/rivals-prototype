# PWA support

## Implementation plan

1. Provide a public web app manifest, a stable application ID, standalone display,
   a dashboard launch URL and PNG icons (192px, 512px, maskable and Apple touch).
2. Register a service worker from `/service-worker.js` with scope `/`. Serve it
   through Django so WhiteNoise's immutable static caching does not delay updates.
3. Precache only a self-contained public offline page. Fetch navigations from the
   network, falling back on connection failure. Keep live scores, authenticated
   pages, API calls, HTMX fragments and writes out of service worker storage.
4. Verify routing, manifest assets, offline behavior, cache cleanup and production
   static collection. Check installation on actual supported browsers.

## Behavior and deployment

No new dependencies or environment variables are required. Existing Docker
`collectstatic` includes the icons and registration script. HTTPS is required
outside localhost. Every page using the base layout advertises the manifest and
registers the worker. The installed app starts at `/dashboard/`; existing Django
authentication redirects signed-out users to login.

Use the browser's Install app control in Chrome/Edge, or Share → Add to Home
Screen in Safari on iOS. Installation controls and eligibility vary by browser.
The first online visit installs the offline fallback. Offline navigation then
shows a connection message; it does not offer offline scores or queue edits.
Existing HTMX request feedback continues to handle failed in-page requests.
HTTP errors remain visible rather than being replaced with an offline message.

Only caches prefixed `rivalspy-pwa-` are managed by this worker. Bump `CACHE_NAME`
in `templates/pwa/service-worker.js` whenever changing the offline page or cache
policy. A new worker installs in the background and waits for the previous worker's
tabs/windows to close before activating, then removes older RivalSpy PWA caches.
It does not force a reload while someone is editing a planner.

Regenerate the geometric radar icons with `python bin/generate-pwa-icons.py`.

## Verification

```sh
.venv/bin/python -m pytest tests/unit/test_pwa.py
node --test tests/js/pwa.test.cjs
python manage.py collectstatic --noinput
```

On localhost or staging over HTTPS:

- Inspect DevTools → Application → Manifest for the name, icons and start URL.
- Verify Service Workers shows `/service-worker.js` controlling scope `/` after
  the first online visit. Cache Storage should contain only `/offline/`.
- Install the app and launch it; verify login and dashboard navigation.
- In DevTools, enable Offline and navigate/reload: the offline page should appear.
  Restore connectivity and reload: the current page should return.
- Confirm an HTMX request failure uses the normal request error UI and a POST
  is never answered with offline HTML. Check login/logout across two accounts;
  no authenticated response should appear in Cache Storage.
- Deploy a changed cache version with a tab open. Verify the worker waits, then
  activates and cleans its old cache after closing all app tabs/windows.

Reference: [MDN: Making PWAs installable](https://developer.mozilla.org/en-US/docs/Web/Progressive_web_apps/Guides/Making_PWAs_installable).
