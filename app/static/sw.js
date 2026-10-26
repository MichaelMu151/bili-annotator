/* BiliAnnotator service worker — app-shell caching only.
 *
 * Strategy:
 *  - App shell (pages, CSS, icons, manifest): stale-while-revalidate so the
 *    phone opens instantly even on a flaky connection, and stays usable for
 *    reading the current card.
 *  - API (/api/*): NETWORK-ONLY. Annotations must reach the server; we never
 *    serve a stale/forged API response. When offline the UI already keeps a
 *    localStorage draft and will retry on reconnect.
 */
const CACHE = "bili-annotator-v2";
const SHELL = [
  "/",
  "/codebook",
  "/progress",
  "/static/style.css",
  "/static/manifest.webmanifest",
  "/static/icon-192.png",
  "/static/icon-512.png",
  "/static/icon-maskable-512.png",
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) =>
      // cache each shell entry independently so one failure can't abort install
      Promise.all(
        SHELL.map((u) => c.add(u).catch(() => {}))
      )
    ).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  const url = new URL(req.url);

  // Never intercept API or non-GET — always go to network.
  if (req.method !== "GET" || url.pathname.startsWith("/api/")) {
    return;
  }
  // Only handle same-origin.
  if (url.origin !== self.location.origin) return;

  e.respondWith(
    caches.open(CACHE).then((cache) =>
      fetch(req)
        .then((resp) => {
          if (resp && resp.status === 200) cache.put(req, resp.clone());
          return resp;
        })
        .catch(() =>
          cache.match(req).then((hit) => hit || caches.match("/"))
        )
    )
  );
});
