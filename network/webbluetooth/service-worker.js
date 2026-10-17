importScripts("./version.js");
const { version: APP_VERSION, cacheId: CACHE_ID } = globalThis.AQUACULTURE_VERSION;
const CACHE_NAME = CACHE_ID + "-v" + APP_VERSION;

const SHELL = ["./", "./index.html", "./style.css", "./app.js", "./ble.js", "./offline.js", "./version.js", "./manifest.webmanifest", "./icons/aquaculture.svg", "./icons/icon-192.png", "./icons/icon-512.png"];
const absolute = (path) => new URL(path, self.registration.scope).href;
const allowed = new Set(SHELL.map(absolute));
const metadata = absolute("./__offline_metadata__");

async function prepareCache() {
  // Do not fill an old version's cache with a newly deployed version's assets.
  const versionResponse = await fetch(new Request(absolute("./version.js"), { cache: "no-store" }));
  const versionSource = await versionResponse.text();
  if (!versionResponse.ok || !versionSource.includes('version: "' + APP_VERSION + '"')) {
    return { cacheId: CACHE_ID, version: APP_VERSION, ready: false, state: "UPDATE_AVAILABLE",
             message: "A new frontend version is available. Install it before repairing offline files." };
  }
  const cache = await caches.open(CACHE_NAME);
  await cache.addAll(SHELL.map((path) => new Request(absolute(path), { cache: "reload" })));
  await cache.put(metadata, new Response(JSON.stringify({ version: APP_VERSION, updatedAt: new Date().toISOString() }), { headers: { "Content-Type": "application/json" } }));
  return verifyCache();
}

async function verifyCache() {
  if (!await caches.has(CACHE_NAME)) return { cacheId: CACHE_ID, version: APP_VERSION, ready: false, state: "NOT_INSTALLED", updatedAt: null, missing: SHELL };
  const cache = await caches.open(CACHE_NAME);
  const missing = [];
  for (const path of SHELL) {
    const entry = await cache.match(absolute(path));
    if (!entry || !entry.ok) missing.push(path);
  }
  const saved = await cache.match(metadata);
  let updatedAt = null;
  if (saved) {
    try { const data = await saved.json(); if (data.version === APP_VERSION && !Number.isNaN(Date.parse(data.updatedAt))) updatedAt = data.updatedAt; } catch { /* No invented timestamp. */ }
  }
  return { cacheId: CACHE_ID, version: APP_VERSION, ready: missing.length === 0,
           state: missing.length ? "ERROR" : "READY", updatedAt, missing,
           message: missing.length ? "Required offline files are missing. Prepare offline access while online." : "Ready for offline use. All required files are cached." };
}

self.addEventListener("install", (event) => {
  event.waitUntil(prepareCache().then((result) => { if (!result.ready) throw new Error("Application cache is incomplete."); }));
});
self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((key) =>
    (key.startsWith(CACHE_ID + "-v") || key.startsWith("aquaculture-setup-")) && key !== CACHE_NAME
  ).map((key) => caches.delete(key)))).then(() => self.clients.claim()));
});
self.addEventListener("message", (event) => {
  if (event.data?.type === "SKIP_WAITING") { self.skipWaiting(); return; }
  if (!["CHECK_CACHE", "PREPARE_CACHE"].includes(event.data?.type) || !event.ports?.[0]) return;
  const reply = event.ports[0];
  event.waitUntil((async () => {
    try { reply.postMessage(await (event.data.type === "PREPARE_CACHE" ? prepareCache() : verifyCache())); }
    catch { reply.postMessage({ cacheId: CACHE_ID, version: APP_VERSION, ready: false, state: "ERROR", updatedAt: null, message: "Offline files could not be prepared. Check your connection and browser storage." }); }
    finally { reply.close(); }
  })());
});
self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin || url.search) return;
  if (event.request.mode === "navigate" && allowed.has(url.href)) {
    event.respondWith(caches.open(CACHE_NAME).then((cache) => cache.match(absolute("./index.html"))).then((cached) => cached || fetch(event.request)));
  } else if (allowed.has(url.href)) {
    event.respondWith(caches.open(CACHE_NAME).then((cache) => cache.match(event.request)).then((cached) => cached || fetch(event.request)));
  }
});
