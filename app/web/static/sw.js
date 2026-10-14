// ledger-agent service worker: keeps a copy of the pages you open so the app still shows your
// last-synced numbers when the Mac is asleep or off. Asking questions and syncing need the Mac.
const CACHE = "ledger-v2";
const ASSETS = ["/static/tailwind.js", "/static/htmx.min.js", "/static/chart.umd.min.js", "/static/icon-180.png"];
const CORE = ["/", "/transactions", "/goals", "/runs"];  // refreshed in the background whenever the Mac is reachable
let lastRefresh = 0;

self.addEventListener("install", (e) => {
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(ASSETS)));
});
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

async function stamped(res) {
  const headers = new Headers(res.headers);
  headers.set("x-cached-at", new Date().toISOString());
  return new Response(await res.clone().text(), { status: res.status, headers });
}

async function refreshCore() {
  if (Date.now() - lastRefresh < 5 * 60 * 1000) return;
  lastRefresh = Date.now();
  const c = await caches.open(CACHE);
  for (const p of CORE) {
    try {
      const r = await fetch(p, { credentials: "same-origin" });
      if (r.ok && !r.redirected) await c.put(p, await stamped(r));
    } catch (_) { /* offline */ }
  }
}

function ago(iso) {
  if (!iso) return "earlier";
  const m = Math.round((Date.now() - new Date(iso)) / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  return h < 48 ? `${h}h ago` : `${Math.round(h / 24)} days ago`;
}

function offlineBanner(when) {
  return `<div style="position:sticky;top:0;z-index:50;background:#fef3c7;color:#92400e;font:14px system-ui;padding:8px 16px;text-align:center">
    Mac offline · showing data from ${ago(when)}. Questions and sync work when it's back.</div>
    <script>window.LEDGER_OFFLINE = true;</script>`;
}

self.addEventListener("fetch", (e) => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== location.origin) return;

  if (url.pathname.startsWith("/static/")) {
    e.respondWith(caches.match(req).then((hit) => hit || fetch(req).then((res) => {
      const copy = res.clone();
      caches.open(CACHE).then((c) => c.put(req, copy));
      return res;
    })));
    return;
  }
  if (req.mode !== "navigate") return;

  const key = url.pathname + url.search;
  e.respondWith((async () => {
    try {
      const res = await fetch(req);
      const html = (res.headers.get("content-type") || "").includes("text/html");
      if (res.ok && !res.redirected && html && !url.pathname.startsWith("/login")) {
        const c = await caches.open(CACHE);
        await c.put(key, await stamped(res));
        e.waitUntil(refreshCore());
      }
      return res;
    } catch (_) {
      const c = await caches.open(CACHE);
      const hit = (await c.match(key)) || (await c.match(url.pathname)) || (await c.match("/"));
      if (!hit) {
        return new Response(`<meta name=viewport content="width=device-width,initial-scale=1">
          <p style="font:16px system-ui;padding:24px">Your Mac is offline and this page hasn't been saved on this phone yet.
          Open it once while the Mac is on.</p>`, { headers: { "content-type": "text/html" } });
      }
      const body = (await hit.text()).replace(/<body([^>]*)>/, (m) => m + offlineBanner(hit.headers.get("x-cached-at")));
      return new Response(body, { headers: { "content-type": "text/html; charset=utf-8" } });
    }
  })());
});

self.addEventListener("message", (e) => {
  if (e.data === "clear") caches.delete(CACHE);
});
