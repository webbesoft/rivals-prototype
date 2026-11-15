// Bump the version when changing the offline page or caching policy.
const CACHE_NAME = "rivalspy-pwa-v1";
const OFFLINE_URL = "{{ offline_url|escapejs }}";

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) =>
      cache.add(new Request(OFFLINE_URL, { cache: "reload", credentials: "omit" }))
    )
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys
      .filter((key) => key.startsWith("rivalspy-pwa-") && key !== CACHE_NAME)
      .map((key) => caches.delete(key)));
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  // HTMX fragments, API calls, form submissions and external requests pass through.
  if (request.method !== "GET" || request.mode !== "navigate" ||
      new URL(request.url).origin !== self.location.origin) return;

  event.respondWith((async () => {
    try {
      // Never store pages, authentication responses or live FPL data.
      return await fetch(request);
    } catch (error) {
      const cache = await caches.open(CACHE_NAME);
      const fallback = await cache.match(OFFLINE_URL);
      if (fallback) return fallback;
      throw error;
    }
  })());
});
