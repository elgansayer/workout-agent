// Service worker for the Angular frontend.
//
// Security rule: personalised responses are never written to CacheStorage.
// Navigations and sensitive/API routes always use the network. The only
// non-network fallback is an explicitly safe, user-neutral offline shell.
// Runtime caching is limited to same-origin build assets with a content hash in
// their filename, which makes them immutable across deployments.
const CACHE_PREFIX = "workout-agent-";
const CACHE = "workout-agent-static-v3";
const OFFLINE_URL = "/offline.html";
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

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(() =>
        caches.match(OFFLINE_URL).then((response) => response || Response.error()),
      ),
    );
    return;
  }

  if (isSensitivePath(url.pathname)) return;
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

self.addEventListener("push", function (event) {
  let payload = event.data ? event.data.text() : "no payload";
  let title = "Workout Agent";
  let options = {
    body: payload,
    icon: "/favicon.ico",
    badge: "/favicon.ico",
    data: { url: "/" },
  };

  try {
    const data = JSON.parse(payload);
    if (data.title) title = data.title;
    if (data.body) options.body = data.body;
    if (data.url) options.data.url = data.url;
  } catch (e) {
    // Plain-text notifications are valid payloads.
  }

  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  const urlToOpen = event.notification.data.url || "/";

  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((windowClients) => {
      for (let i = 0; i < windowClients.length; i++) {
        const client = windowClients[i];
        if (client.url === urlToOpen && "focus" in client) return client.focus();
      }
      if (clients.openWindow) return clients.openWindow(urlToOpen);
    }),
  );
});
