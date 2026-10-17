// Real local Chromium UI/worker checks; no Bluetooth or Pi hardware is simulated.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { readFile, mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { resolve, join, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout as delay } from "node:timers/promises";

const frontend = fileURLToPath(new URL("../webbluetooth/", import.meta.url));
const output = fileURLToPath(new URL("../.qa/", import.meta.url));
await mkdir(output, { recursive: true });
const profile = await mkdtemp(join(output, "browser-profile-"));
const assets = new Set(["index.html", "app.js", "ble.js", "offline.js", "version.js", "style.css", "manifest.webmanifest", "service-worker.js", "icons/aquaculture.svg", "icons/icon-192.png", "icons/icon-512.png"]);
const mime = { html: "text/html", js: "text/javascript", css: "text/css", svg: "image/svg+xml", png: "image/png", webmanifest: "application/manifest+json" };
let servedVersion = "1.1.0";
const server = createServer(async (request, response) => {
  const name = new URL(request.url, "http://localhost").pathname.slice(1) || "index.html";
  if (!assets.has(name)) { response.writeHead(404); response.end(); return; }
  try { let data = await readFile(join(frontend, name)); if (name === "version.js") data = data.toString().replace('version: "1.1.0"', 'version: "' + servedVersion + '"'); response.writeHead(200, { "Content-Type": mime[name.split(".").pop()] || "application/octet-stream", "Cache-Control": "no-cache" }); response.end(data); }
  catch { response.writeHead(500); response.end(); }
});
await new Promise((done) => server.listen(0, "127.0.0.1", done));
const url = `http://127.0.0.1:${server.address().port}/`;
const executable = process.env.AQUACULTURE_QA_BROWSER || "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const browser = spawn(executable, ["--headless=new", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "--no-first-run", "--no-default-browser-check", "--disable-background-networking", "about:blank"], { windowsHide: true, stdio: "ignore" });
let ws;
try {
  let port;
  for (let attempt = 0; attempt < 80; attempt++) {
    try { port = Number((await readFile(join(profile, "DevToolsActivePort"), "utf8")).split("\n")[0]); break; } catch { await delay(250); }
  }
  assert.ok(port, "Chromium debugging endpoint did not start");
  const target = await (await fetch(`http://127.0.0.1:${port}/json/new?${encodeURIComponent(url)}`, { method: "PUT" })).json();
  ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((done, fail) => { ws.addEventListener("open", done, { once: true }); ws.addEventListener("error", fail, { once: true }); });
  let serial = 0;
  const pending = new Map(), exceptions = [];
  ws.addEventListener("message", ({ data }) => {
    const message = JSON.parse(data);
    if (message.method === "Runtime.exceptionThrown") exceptions.push(message.params.exceptionDetails.text);
    const item = pending.get(message.id);
    if (item) { pending.delete(message.id); clearTimeout(item.timer); message.error ? item.reject(new Error(JSON.stringify(message.error))) : item.resolve(message.result); }
  });
  function cdp(method, params = {}) {
    const id = ++serial;
    return new Promise((resolveResult, reject) => {
      const timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }, 15000);
      pending.set(id, { resolve: resolveResult, reject, timer }); ws.send(JSON.stringify({ id, method, params }));
    });
  }
  async function evaluate(expression) {
    const result = await cdp("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
    return result.result.value;
  }
  async function waitLoaded(expectedVersion = null) {
    for (let attempt = 0; attempt < 80; attempt++) {
      try { if (await evaluate("document.readyState === 'complete' && document.querySelector('#cacheState')?.textContent==='READY'" + (expectedVersion ? " && document.querySelector('#cacheVersion')?.textContent===" + JSON.stringify("v" + expectedVersion) : ""))) return; } catch { /* Navigation replaces execution context. */ }
      await delay(100);
    }
    throw new Error("Page/service-worker did not finish loading");
  }
  await cdp("Page.enable"); await cdp("Runtime.enable"); await cdp("Network.enable");
  await cdp("Page.navigate", { url }); await waitLoaded();
  assert.equal(await evaluate("document.querySelector('#privilegedNetwork').hidden"), true);
  await evaluate("document.querySelector('#authorizationFormCard').hidden=false; document.querySelector('#showAdminPassword').click()");
  assert.equal(await evaluate("document.querySelector('#adminPassword').type"), "text");
  await evaluate("document.querySelector('#showAdminPassword').click()");
  assert.equal(await evaluate("document.querySelector('#adminPassword').type"), "password");
  for (const width of [320, 390, 768, 1280]) {
    await cdp("Emulation.setDeviceMetricsOverride", { width, height: 900, deviceScaleFactor: 1, mobile: width < 650 });
    for (const theme of ["light", "dark"]) {
      await evaluate(`document.documentElement.dataset.theme = '${theme}'`);
      const dimensions = await evaluate("({scroll: document.documentElement.scrollWidth, viewport: innerWidth})");
      assert.ok(dimensions.scroll <= dimensions.viewport, `Horizontal overflow at ${width}px ${theme}: ${JSON.stringify(dimensions)}`);
    }
  }
  await evaluate("document.querySelector('#wifiFields').disabled=false; document.querySelector('#showPassword').click()");
  assert.equal(await evaluate("document.querySelector('#password').type"), "text");
  await evaluate("document.querySelector('#showPassword').click()");
  assert.equal(await evaluate("document.querySelector('#password').type"), "password");
  await evaluate("document.querySelector('#wifiFields').disabled=true");
  for (const [name, width, theme] of [["mobile-light", 390, "light"], ["desktop-dark", 1280, "dark"]]) {
    await cdp("Emulation.setDeviceMetricsOverride", { width, height: 900, deviceScaleFactor: 1, mobile: width < 650 });
    await evaluate(`document.documentElement.dataset.theme='${theme}'`);
    const screenshot = await cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
    await writeFile(join(output, `${name}.png`), Buffer.from(screenshot.data, "base64"));
  }
  // Delete one actual shell entry and verify the foreground notices before repair.
  await evaluate("(async()=>{const name=(await caches.keys()).find(n=>n.startsWith('aquaculture-network-setup-v'));await (await caches.open(name)).delete(new URL('./ble.js',location.href).href);document.querySelector('#checkOffline').click()})()");
  for (let i=0;i<50 && !await evaluate("document.querySelector('#cacheState').textContent==='ERROR'");i++) await delay(100);
  assert.equal(await evaluate("document.querySelector('#cacheState').textContent"), "ERROR");
  await evaluate("document.querySelector('#prepareOffline').click()"); await waitLoaded();
  // Serve a new version without editing the workspace; Chromium must install it.
  servedVersion = "1.1.1";
  await evaluate("(async()=>{await (await navigator.serviceWorker.getRegistration()).update()})()");
  for (let i=0;i<100 && !await evaluate("Boolean(document.querySelector('#update') && !document.querySelector('#update').hidden)");i++) await delay(100);
  assert.equal(await evaluate("document.querySelector('#cacheState').textContent"), "UPDATE_AVAILABLE");
  await evaluate("document.querySelector('#update').click()").catch(() => {}); await waitLoaded("1.1.1");
  assert.equal(await evaluate("document.querySelector('#cacheVersion').textContent"), "v1.1.1");
  assert.equal(await evaluate("(async()=>!(await caches.keys()).includes('aquaculture-network-setup-v1.1.0'))()"), true);
  // Force unsupported-browser detection before the module initializes, while offline.
  await cdp("Page.addScriptToEvaluateOnNewDocument", { source: "Object.defineProperty(navigator, 'bluetooth', {value: undefined, configurable: true})" });
  await cdp("Network.emulateNetworkConditions", { offline: true, latency: 0, downloadThroughput: 0, uploadThroughput: 0 });
  await cdp("Page.reload"); await waitLoaded();
  assert.equal(await evaluate("navigator.onLine"), false);
  assert.equal(await evaluate("Boolean(navigator.serviceWorker.controller)"), true);
  assert.equal(await evaluate("!document.querySelector('#compatibility').hidden && document.querySelector('#connect').disabled"), true);
  await cdp("Page.addScriptToEvaluateOnNewDocument", { source: "delete Navigator.prototype.serviceWorker" });
  await cdp("Page.reload");
  for (let i=0;i<80;i++) {
    try { if (await evaluate("document.querySelector('#cacheState')?.textContent==='NOT_SUPPORTED'")) break; } catch {}
    await delay(100);
  }
  assert.equal(await evaluate("document.querySelector('#cacheState').textContent"), "NOT_SUPPORTED");
  assert.equal(exceptions.length, 0, exceptions.join("; "));
  console.log("Browser checks passed: responsive themes, Admin/Wi-Fi eyes, unauthorized UI hidden, cache completeness/repair, version update/obsolete-cache cleanup, offline reload, unsupported BLE/SW guidance, no runtime exceptions.");
  console.log(`Screenshots: ${output}`);
  await cdp("Browser.close").catch(() => {});
} finally {
  ws?.close();
  browser.kill();
  await new Promise((done) => server.close(done));
  // Remove only the exact temporary browser profile created above, under the QA directory.
  const checked = resolve(profile), root = resolve(output) + sep;
  assert.ok(checked.startsWith(root) && checked.includes("browser-profile-"));
  for (let attempt = 0; attempt < 10; attempt++) {
    try { await rm(checked, { recursive: true }); break; } catch { await delay(300); }
  }
}
