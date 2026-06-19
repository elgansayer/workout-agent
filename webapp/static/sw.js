// Service worker for the Workout Agent dashboard.
// App-shell caching so the dashboard opens instantly and survives brief
// connection drops. Navigations are network-first (data stays fresh) with a
// cached fallback; static assets are stale-while-revalidate (served instantly
// from cache, then refreshed in the background so new deploys propagate on the
// next load). Bump CACHE whenever you want to force-flush all cached assets.
const CACHE = "workout-agent-v2";
const SHELL = ["/", "/static/style.css", "/static/icon.svg", "/static/manifest.webmanifest"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then((response) => {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(request, copy));
          return response;
        })
        .catch(() => caches.match(request).then((hit) => hit || caches.match("/")))
    );
    return;
  }

  // Stale-while-revalidate: return the cached copy immediately (if any) while
  // fetching a fresh copy in the background for the next load. This means an
  // updated CSS/JS deploy shows up on the second visit without a hard reload.
  event.respondWith(
    caches.match(request).then((hit) => {
      const fetchPromise = fetch(request).then((response) => {
        const copy = response.clone();
        caches.open(CACHE).then((cache) => cache.put(request, copy));
        return response;
      });
      return hit || fetchPromise;
    })
  );
});
