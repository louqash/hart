// hart service worker: makes the app installable, keeps static
// assets (CSS, JS, fonts, icons) cached, and keeps the last copy of each page
// you opened. When the server can't be reached (Tailscale down, server
// restarting), that copy is shown with an "offline" banner. API data is never
// cached, and nothing is shown from the cache while the server answers.
const STATIC_CACHE = "hart-static-v2";
const PAGE_CACHE = "hart-pages-v1";
const NO_OFFLINE_COPY = ["/chat", "/api/", "/mcp", "/static/", "/sw.js", "/manifest.webmanifest", "/healthz"];

self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => ![STATIC_CACHE, PAGE_CACHE].includes(k)).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

function offlineCopy(response, savedAt) {
  const when = new Date(savedAt).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });
  return response.text().then((html) => new Response(
    html.replace("<main>", `<main><div class="offline-banner">Offline — showing this page as it was on ${when}. `
      + `It updates when the server is reachable again.</div>`),
    { headers: { "Content-Type": "text/html; charset=utf-8" } },
  ));
}

async function page(request) {
  const cache = await caches.open(PAGE_CACHE);
  try {
    const response = await fetch(request);
    if (response.ok && (response.headers.get("Content-Type") || "").includes("text/html")) {
      const copy = new Response(await response.clone().blob(), {
        headers: { "Content-Type": "text/html; charset=utf-8", "X-Tri-Saved-At": String(Date.now()) },
      });
      cache.put(request.url, copy);
      return response;
    }
    if (response.status < 500) return response;
    throw new Error(`server answered ${response.status}`);
  } catch (err) {
    const saved = await cache.match(request.url);
    if (saved) return offlineCopy(saved, Number(saved.headers.get("X-Tri-Saved-At")) || Date.now());
    return new Response(
      "<!doctype html><meta name=viewport content='width=device-width'><body style='background:#0a140f;color:#f2e8d5;"
      + "font:16px system-ui;padding:24px'><h2>hart is offline</h2><p>This page hasn't been opened on this device yet, "
      + "so there's no saved copy. Check Tailscale and try again.</p></body>",
      { status: 503, headers: { "Content-Type": "text/html; charset=utf-8" } },
    );
  }
}

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== location.origin) return;
  if (event.request.mode === "navigate") {
    if (NO_OFFLINE_COPY.some((p) => url.pathname.startsWith(p))) return;
    event.respondWith(page(event.request));
    return;
  }
  if (!url.pathname.startsWith("/static/")) return;  // API and data: always the network
  // Static URLs carry ?v=<mtime>, so a cached response is never stale.
  event.respondWith(
    caches.open(STATIC_CACHE).then(async (cache) => {
      const hit = await cache.match(event.request);
      if (hit) return hit;
      const response = await fetch(event.request);
      if (response.ok) cache.put(event.request, response.clone());
      return response;
    }),
  );
});
