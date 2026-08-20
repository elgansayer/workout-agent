// Service worker for the Workout Agent dashboard.
//
// Security rule: personalised responses are never written to CacheStorage.
// Navigations and sensitive/API routes always use the network. The only
// non-network fallback is an explicitly safe, user-neutral offline shell.
// Runtime caching is limited to same-origin build assets with a content hash in
// their filename, which makes them immutable across deployments.
const CACHE_PREFIX = "workout-agent-";
const CACHE = "workout-agent-static-v3";
const OFFLINE_URL = "/static/offline.html";
const PRECACHE = [OFFLINE_URL];

const SENSITIVE_PATH_PREFIXES = [
  "/api/",
  "/settings",
  "/chat",
  "/history",
  "/progress",
  "/stats",
  "/plan",
  "/programmes",
  "/checkins",
  "/notifications",
  "/metrics",
  "/exports",
  "/export",
  "/login",
  "/logout",
  "/auth",
  "/google-health/",
];

// Angular production builds use outputHashing=all. Only cache files carrying a
// sufficiently long content hash immediately before the extension.
const VERSIONED_ASSET_RE =
  /(?:^|\/)[^/?#]+-[A-Za-z0-9_-]{8,}\.(?:css|js|mjs|png|jpe?g|svg|webp|avif|woff2?|ttf)$/i;

function isSensitivePath(pathname) {
  return SENSITIVE_PATH_PREFIXES.some((prefix) => {
    if (prefix.endsWith("/")) return pathname.startsWith(prefix);
    return pathname === prefix || pathname.startsWith(`${prefix}/`);
  });
}

function isVersionedStaticAsset(url) {
  return url.origin === self.location.origin && VERSIONED_ASSET_RE.test(url.pathname);
}

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(PRECACHE)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  // Purge every older Workout Agent cache, including the previous v2 cache
  // that could contain personalised navigations from another signed-in user.
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((key) => key.startsWith(CACHE_PREFIX) && key !== CACHE)
          .map((key) => caches.delete(key)),
      ),
    ),
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Never cache page navigations. If the network is unavailable, show the
  // static offline shell rather than stale dashboard data from CacheStorage.
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(() =>
        caches.match(OFFLINE_URL).then((response) => response || Response.error()),
      ),
    );
    return;
  }

  // API, authentication, health-data and account-specific requests bypass the
  // service worker completely. This remains true even if a future endpoint's
  // filename happens to resemble a versioned asset.
  if (isSensitivePath(url.pathname)) return;

  // Everything else is network-only unless it is an immutable, content-hashed
  // build asset. In particular, unversioned HTML/JSON is never cached.
  if (!isVersionedStaticAsset(url)) return;

  event.respondWith(
    caches.open(CACHE).then((cache) =>
      cache.match(request).then((cached) => {
        if (cached) return cached;
        return fetch(request).then((response) => {
          if (!response.ok || response.type !== "basic") return response;
          const copy = response.clone();
          return cache.put(request, copy).then(() => response);
        });
      }),
    ),
  );
});
