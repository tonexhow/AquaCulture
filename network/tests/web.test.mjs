import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import vm from "node:vm";
import { Reassembler, frames, bytes, AquaBluetooth, SERVICE_UUID } from "../webbluetooth/ble.js";
const frontend = new URL("../webbluetooth/", import.meta.url);

test("minimum MTU framing and Unicode roundtrip, malformed/expired messages", () => {
  const data = bytes(JSON.stringify({ ssid: "魚塘 🐟", networks: Array(100).fill("Wi-Fi") }));
  const receiver = new Reassembler(); let value;
  for (const frame of frames(data, 42)) { assert.ok(frame.length <= 20); value = receiver.feed(frame); }
  assert.deepEqual(value, data);
  const parts = frames(bytes("A".repeat(30)), 1);
  receiver.feed(parts[0], 0); assert.throws(() => receiver.feed(parts[1], 31000));
  receiver.feed(parts[0]); assert.throws(() => receiver.feed(parts[0]));
  assert.throws(() => frames(new Uint8Array(32769), 1));
});

test("Admin credentials are blocked until authenticated BLE pairing has succeeded", async () => {
  const client = new AquaBluetooth();
  await assert.rejects(client.authorize("synthetic-password"), /Authenticated, encrypted/);
  await assert.rejects(client.request("configure", { password: "synthetic-wifi" }), /Authenticated Bluetooth/);
  client.securityVerified = true;
  await assert.rejects(client.request("scan"), /Verify the Admin password/);
});

async function workerFixture() {
  const handlers = {}, storage = new Map();
  const scope = "https://setup.example/";
  let versionSource = readFileSync(new URL("version.js", frontend), "utf8");
  const key = (request) => typeof request === "string" ? request : request.url;
  const caches = {
    has: async (name) => storage.has(name),
    keys: async () => [...storage.keys()],
    delete: async (name) => storage.delete(name),
    open: async (name) => {
      if (!storage.has(name)) storage.set(name, new Map());
      const cache = storage.get(name);
      return {
        addAll: async (requests) => { for (const request of requests) { const name = new URL(key(request)).pathname.slice(1) || "index.html"; assert.ok(existsSync(fileURLToPath(new URL(name, frontend)))); cache.set(key(request), new Response(name === "version.js" ? versionSource : "static fixture")); } },
        put: async (request, response) => cache.set(key(request), response.clone()),
        match: async (request) => cache.get(key(request))?.clone(),
        delete: async (request) => cache.delete(key(request))
      };
    }
  };
  let context;
  context = vm.createContext({ URL, Set, Promise, Date, Request, Response, caches,
    fetch: async () => new Response(versionSource),
    importScripts: (file) => vm.runInContext(readFileSync(new URL(file, frontend), "utf8"), context),
    self: { registration: { scope }, location: { origin: "https://setup.example" }, clients: { claim: async () => {} }, addEventListener: (type, callback) => { handlers[type] = callback; } }
  });
  vm.runInContext(readFileSync(new URL("service-worker.js", frontend), "utf8"), context);
  async function message(type) {
    let waited, response;
    handlers.message({ data: { type }, ports: [{ postMessage: (data) => { response = data; }, close: () => {} }], waitUntil: (promise) => { waited = promise; } });
    await waited; return response;
  }
  return { handlers, caches, storage, message, updateVersion: () => { versionSource = versionSource.replace('version: "1.1.0"', 'version: "1.2.0"'); } };
}

test("worker readiness verifies every asset, detects missing files, repairs, and rejects mixed versions", async () => {
  const fixture = await workerFixture();
  assert.equal((await fixture.message("CHECK_CACHE")).state, "NOT_INSTALLED");
  let installed; fixture.handlers.install({ waitUntil: (promise) => { installed = promise; } }); await installed;
  const status = await fixture.message("CHECK_CACHE");
  assert.equal(status.ready, true); assert.equal(status.cacheId, "aquaculture-network-setup");
  assert.equal(status.version, "1.1.0"); assert.ok(!Number.isNaN(Date.parse(status.updatedAt)));
  const cache = await fixture.caches.open("aquaculture-network-setup-v1.1.0");
  await cache.delete("https://setup.example/ble.js");
  const missing = await fixture.message("CHECK_CACHE");
  assert.equal(missing.ready, false); assert.ok(missing.missing.includes("./ble.js"));
  assert.equal((await fixture.message("PREPARE_CACHE")).ready, true);
  fixture.updateVersion();
  const update = await fixture.message("PREPARE_CACHE");
  assert.equal(update.state, "UPDATE_AVAILABLE"); assert.equal(update.ready, false);
});

test("worker removes only obsolete app caches, serves offline navigation, never handles credential requests", async () => {
  const fixture = await workerFixture();
  await fixture.message("PREPARE_CACHE");
  await fixture.caches.open("aquaculture-network-setup-v0");
  await fixture.caches.open("aquaculture-setup-v1");
  await fixture.caches.open("other-app");
  let activated; fixture.handlers.activate({ waitUntil: (promise) => { activated = promise; } }); await activated;
  assert.deepEqual(await fixture.caches.keys(), ["aquaculture-network-setup-v1.1.0", "other-app"]);
  let response;
  fixture.handlers.fetch({ request: { method: "GET", url: "https://setup.example/", mode: "navigate" }, respondWith: (value) => { response = value; } });
  assert.ok((await response).ok);
  for (const request of [{ method: "POST", url: "https://setup.example/auth" }, { method: "GET", url: "https://setup.example/?token=secret", mode: "navigate" }, { method: "GET", url: "https://other.example/" }]) {
    response = undefined;
    fixture.handlers.fetch({ request, respondWith: (value) => { response = value; } });
    assert.equal(response, undefined);
  }
});

test("offline readiness requires actual control, and unsupported browsers are reported", async () => {
  const OfflineAccess = (await import("../webbluetooth/offline.js")).OfflineAccess;
  const originalNavigator = Object.getOwnPropertyDescriptor(globalThis, "navigator");
  globalThis.window = { isSecureContext: true };
  Object.defineProperty(globalThis, "navigator", { value: {}, configurable: true });
  globalThis.caches = {};
  assert.equal((await new OfflineAccess().check()).state, "NOT_SUPPORTED");
  Object.defineProperty(globalThis, "navigator", { value: { serviceWorker: { getRegistration: async () => ({ active: {} }), controller: null } }, configurable: true });
  assert.equal((await new OfflineAccess().check()).state, "NOT_INSTALLED");
  delete globalThis.window; delete globalThis.caches;
  if (originalNavigator) Object.defineProperty(globalThis, "navigator", originalNavigator);
});

test("frontend manifest, UUID and credential storage boundaries", () => {
  const manifest = JSON.parse(readFileSync(new URL("manifest.webmanifest", frontend)));
  for (const icon of manifest.icons) assert.ok(existsSync(fileURLToPath(new URL(icon.src, frontend))));
  assert.equal(manifest.display, "standalone");
  const python = readFileSync(new URL("../rpi/protocol.py", frontend), "utf8");
  assert.ok(python.includes(SERVICE_UUID));
  const app = readFileSync(new URL("app.js", frontend), "utf8");
  assert.ok(!/sessionStorage|indexedDB|console\./.test(app));
  assert.equal((app.match(/localStorage\.setItem/g) || []).length, 1); // Theme preference only.
  const vercel = JSON.parse(readFileSync(new URL("vercel.json", frontend)));
  assert.equal(vercel.outputDirectory, ".");
});
