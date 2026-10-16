import { AquaBluetooth } from "./ble.js";
import { OfflineAccess } from "./offline.js";
import "./version.js";

const $ = (id) => document.getElementById(id);
const bluetooth = new AquaBluetooth();
const compatible = window.isSecureContext && Boolean(navigator.bluetooth) && Boolean(globalThis.crypto?.randomUUID);
const offline = new OfflineAccess();
let authRetryUntil = 0;
let retryTimer;
let busy = false;
const labels = {
  IDLE: "Ready", SCANNING: "Scanning nearby Wi-Fi networks…", SCAN_COMPLETE: "Wi-Fi scan complete.",
  CONNECTING: "Connecting to Wi-Fi…", AUTHENTICATING: "Authenticating with the Wi-Fi network…",
  OBTAINING_IP: "Waiting for an IP address…", CONNECTED_LOCAL: "Connected to Wi-Fi. Internet access is unavailable or unknown.",
  CONNECTED_ONLINE: "Connected to Wi-Fi with internet access.", DISCONNECTED: "The Pi is disconnected from Wi-Fi.",
  FAILED: "Connection failed.", TIMED_OUT: "Connection attempt timed out.",
};

function message(text, type = "info", loading = false) {
  $("message").textContent = text;
  $("message").className = `notice ${type}${loading ? " busy" : ""}`;
}
function controls() {
  const authorized = bluetooth.authorized;
  document.querySelectorAll("[data-authorized]").forEach((button) => { button.disabled = busy || !authorized; });
  $("wifiFields").disabled = busy || !authorized;
  $("authorize").disabled = busy || !bluetooth.securityVerified || Date.now() < authRetryUntil;
  $("connect").disabled = busy || !compatible;
  $("disconnect").hidden = !bluetooth.device?.gatt?.connected;
  $("connect").hidden = Boolean(bluetooth.device?.gatt?.connected);
  $("authorizationFormCard").hidden = !bluetooth.device?.gatt?.connected || authorized;
  $("privilegedNetwork").hidden = !authorized;
  $("bluetoothStatus").textContent = authorized ? "Bluetooth connected · Admin verified" : bluetooth.device?.gatt?.connected ? "Bluetooth connected · Admin verification required" : "Bluetooth disconnected";
  $("bluetoothStatus").className = `badge ${authorized ? "success" : "neutral"}`;
  document.querySelectorAll(".network").forEach((button) => { button.disabled = busy || !authorized || button.dataset.supported === "false"; });
}
async function action(work) {
  if (busy) return;
  busy = true; controls();
  try { await work(); } catch (error) {
    if (error.retryAfter) {
      authRetryUntil = Date.now() + error.retryAfter * 1000;
      clearInterval(retryTimer);
      retryTimer = setInterval(() => {
        const seconds = Math.max(0, Math.ceil((authRetryUntil - Date.now()) / 1000));
        $("authenticationStatus").textContent = seconds ? `Authentication limited. Try again in ${seconds} seconds.` : "You can verify the Admin password again.";
        controls(); if (!seconds) clearInterval(retryTimer);
      }, 1000);
      $("authenticationStatus").textContent = `Authentication limited. Try again in ${error.retryAfter} seconds.`;
    } else if (!bluetooth.authorized) $("authenticationStatus").textContent = "Unable to verify the Admin password.";
    message(error.name === "NotFoundError" ? "Device selection canceled. Bring the Pi nearby and try again." : error.message || "The request failed. Reconnect and try again.", "danger");
    $("recovery").hidden = !bluetooth.authorized;
  } finally { busy = false; controls(); }
}
function fields(target, rows) {
  target.replaceChildren();
  for (const [name, value] of rows) {
    const dt = document.createElement("dt"), dd = document.createElement("dd");
    dt.textContent = name; dd.textContent = value === null || value === undefined || value === "" ? "Unavailable" : String(value);
    target.append(dt, dd);
  }
}
function showNetwork(network) {
  fields($("networkInfo"), [["Wi-Fi", labels[network.state] || network.state], ["Network", network.ssid], ["IP address", network.ip_address],
    ["Signal", network.signal === null || network.signal === undefined ? null : `${network.signal}%`], ["Internet", network.internet_available === true ? "Available (tested)" : network.internet_available === false ? "Unavailable / captive portal" : "Unknown / not tested"],
    ["Local connection", network.local_connectivity ? "IP connection established" : "No IP connection"], ["Wi-Fi adapter", network.adapter],
    ["Security", Array.isArray(network.security) ? network.security.join(" / ") : network.security], ["Last network", network.last_connected_ssid],
    ["Last success", network.last_successful_connection?.connected_at ? new Date(network.last_successful_connection.connected_at).toLocaleString() : null],
    ["Last failure", network.last_failed_connection?.attempted_at ? new Date(network.last_failed_connection.attempted_at).toLocaleString() : null], ["Recent error", network.recent_error],
    ["Saved networks", network.saved_profiles?.map((p) => p.ssid).join(", ") || null]]);
}
async function showHistory() {
  const data = await bluetooth.request("history");
  $("history").replaceChildren();
  if (!data.connections?.length) { const item = document.createElement("li"); item.className = "empty"; item.textContent = "No connection history recorded yet."; $("history").append(item); }
  for (const row of data.connections || []) {
    const item = document.createElement("li"), title = document.createElement("strong"), detail = document.createElement("span");
    title.textContent = row.ssid || "Unnamed network";
    const timestamp = row.connected_at || row.attempted_at;
    detail.textContent = `${labels[row.status] || row.status}${row.error_category ? ` · ${row.error_category}` : ""}${timestamp ? ` · ${new Date(timestamp).toLocaleString()}` : ""}${row.disconnected_at ? ` · Disconnected ${new Date(row.disconnected_at).toLocaleString()}` : ""}`;
    item.append(title, detail); $("history").append(item);
  }
}
async function refresh() { showNetwork(await bluetooth.request("status")); await showHistory(); }
async function scan() {
  message("Scanning nearby Wi-Fi networks…", "info", true);
  const data = await bluetooth.request("scan");
  $("networks").replaceChildren();
  if (!data.networks?.length) { const empty = document.createElement("p"); empty.className = "empty"; empty.textContent = "No visible networks found. Scan again or enter a hidden SSID below."; $("networks").append(empty); }
  for (const network of data.networks || []) {
    const supported = ["open", "wpa-psk", "sae"].includes(network.security);
    const button = document.createElement("button"), title = document.createElement("strong"), detail = document.createElement("span");
    button.type = "button"; button.className = "network"; button.dataset.supported = String(supported); button.disabled = !supported;
    title.textContent = network.ssid;
    detail.textContent = `${network.signal}% signal · ${network.security}${network.connected ? " · Connected" : ""}${network.saved ? " · Saved" : ""}${!supported ? " · Separate setup required" : ""}`;
    button.append(title, detail);
    button.addEventListener("click", () => {
      $("ssid").value = network.ssid; $("security").value = network.security; $("hiddenNetwork").checked = false; $("password").value = "";
      document.querySelectorAll(".network").forEach((node) => { node.classList.toggle("selected", node === button); node.setAttribute("aria-pressed", String(node === button)); });
      passwordMode(); $("ssid").scrollIntoView({ block: "center", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth" });
    });
    $("networks").append(button);
  }
  message(data.networks?.length ? "Choose a network below, or enter a hidden network manually." : "No networks found. You can enter a hidden SSID manually.");
}
function passwordMode() {
  const open = $("security").value === "open";
  $("passwordGroup").hidden = open;
  if (open) $("password").value = "";
}
$("connect").addEventListener("click", () => action(async () => { await bluetooth.connect(); message("Authenticated Bluetooth link established. Enter the current AquaCulture Admin password."); $("authenticationStatus").textContent = "The protected Bluetooth link is ready. Your password is verified locally on the Pi."; }));
$("disconnect").addEventListener("click", () => { bluetooth.disconnect(); controls(); message("Bluetooth disconnected. You can reconnect at any time."); });
$("authorizationForm").addEventListener("submit", (event) => {
  event.preventDefault();
  action(async () => {
    let password = $("adminPassword").value; $("adminPassword").value = "";
    $("adminPassword").type = "password";
    $("showAdminPassword").setAttribute("aria-pressed", "false");
    $("showAdminPassword").setAttribute("aria-label", "Show Admin password");
    message("Verifying the Admin password locally on the Pi…", "info", true);
    try { const verification = bluetooth.authorize(password); password = null; await verification; } finally { password = null; }
    $("authenticationStatus").textContent = "Admin verified.";
    const device = await bluetooth.request("info");
    fields($("deviceInfo"), [["Device", device.device_name], ["Hostname", device.hostname], ["Identifier", device.device_id], ["Model", device.model], ["Operating system", device.operating_system],
      ["Uptime", device.uptime_seconds === null ? null : `${Math.floor(device.uptime_seconds / 3600)}h ${Math.floor(device.uptime_seconds % 3600 / 60)}m`], ["Service version", device.service_version], ["Protocol", device.protocol_version]]);
    showNetwork(device.network); await showHistory(); message("Admin verified. Scan for Wi-Fi or enter a hidden network below.", "success");
  });
});
$("refresh").addEventListener("click", () => action(refresh));
$("scan").addEventListener("click", () => action(scan));
$("scanAgain").addEventListener("click", () => action(scan));
$("retry").addEventListener("click", () => { $("password").focus(); message("Re-enter the password if required, then choose Connect to Wi-Fi. The previous password was cleared."); });
$("security").addEventListener("change", passwordMode);
$("ssid").addEventListener("input", () => { document.querySelectorAll(".network").forEach((node) => { node.classList.remove("selected"); node.setAttribute("aria-pressed", "false"); }); });
$("showPassword").addEventListener("click", () => {
  const visible = $("password").type === "password";
  $("password").type = visible ? "text" : "password";
  $("showPassword").setAttribute("aria-label", visible ? "Hide Wi-Fi password" : "Show Wi-Fi password");
  $("showPassword").setAttribute("aria-pressed", String(visible));
});
$("showAdminPassword").addEventListener("click", () => {
  const visible = $("adminPassword").type === "password";
  $("adminPassword").type = visible ? "text" : "password";
  $("showAdminPassword").setAttribute("aria-label", visible ? "Hide Admin password" : "Show Admin password");
  $("showAdminPassword").setAttribute("aria-pressed", String(visible));
});
$("wifiForm").addEventListener("submit", (event) => {
  event.preventDefault();
  action(async () => {
    const ssid = $("ssid").value, mode = $("security").value;
    let password = $("password").value;
    const ssidBytes = new TextEncoder().encode(ssid).length, length = new TextEncoder().encode(password).length;
    if (!ssidBytes || ssidBytes > 32 || /[\x00-\x1f\x7f]/.test(ssid)) throw new Error("Network name must be 1–32 UTF-8 bytes without control characters.");
    if (mode === "wpa-psk" && !(length >= 8 && length <= 63 || /^[0-9a-f]{64}$/i.test(password))) throw new Error("WPA2 passwords must be 8–63 UTF-8 bytes, or a 64-digit hexadecimal key.");
    if (mode === "sae" && !(length >= 1 && length <= 63)) throw new Error("WPA3 passwords must be 1–63 UTF-8 bytes.");
    if (password.includes("\0")) throw new Error("Password cannot contain NUL characters.");
    $("password").value = ""; $("password").type = "password";
    $("showPassword").setAttribute("aria-pressed", "false"); $("showPassword").setAttribute("aria-label", "Show Wi-Fi password");
    $("recovery").hidden = true;
    message(`Connecting the Pi to ${ssid}…`, "warning", true);
    try {
      const connection = bluetooth.request("configure", { ssid, security: mode, password: mode === "open" ? "" : password, hidden: $("hiddenNetwork").checked });
      password = null;
      const result = await connection;
      showNetwork(result); await showHistory();
      message(`Connected to ${result.ssid}. IP: ${result.ip_address || "unavailable"}. ${result.internet_available === true ? "Internet is available." : "Internet is unavailable or has not been verified."}`, "success");
    } finally { password = null; }
  });
});
bluetooth.addEventListener("status", ({ detail }) => {
  if (detail.type === "network") { showNetwork(detail.payload); return; }
  if (detail.type === "progress") message(labels[detail.status] || detail.status, "warning", true);
});
bluetooth.addEventListener("disconnected", () => {
  $("password").value = ""; $("adminPassword").value = "";
  fields($("deviceInfo"), [["Device", "Reconnect to retrieve current information"]]);
  fields($("networkInfo"), [["Network", "Unknown while Bluetooth is disconnected"]]);
  $("history").replaceChildren(); $("networks").replaceChildren();
  controls(); message("Bluetooth disconnected or session expired. Reconnect and authorize again.", "warning");
});
bluetooth.addEventListener("authorizationrequired", ({ detail }) => {
  $("adminPassword").value = ""; $("password").value = "";
  controls(); message(detail, "warning");
});

const preferred = matchMedia("(prefers-color-scheme: dark)");
let theme;
try { theme = localStorage.getItem("aquaculture-setup-theme"); } catch { /* Storage can be disabled. */ }
function applyTheme(value) { document.documentElement.dataset.theme = value; $("theme").setAttribute("aria-label", `Switch to ${value === "dark" ? "light" : "dark"} theme`); }
applyTheme(theme || (preferred.matches ? "dark" : "light"));
$("theme").addEventListener("click", () => { const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark"; applyTheme(next); try { localStorage.setItem("aquaculture-setup-theme", next); } catch { /* Optional theme preference only. */ } });
if (!compatible) {
  $("compatibility").hidden = false;
  $("compatibility").textContent = "Bluetooth setup requires Android Chrome (initial supported target) or another compatible Web Bluetooth browser, served over HTTPS or localhost. Safari on iPhone and Firefox do not provide this required interface. Open this page in a compatible browser.";
  $("connect").disabled = true;
}

offline.addEventListener("change", ({ detail }) => {
  $("cacheState").textContent = detail.state;
  $("cacheState").className = "badge " + (detail.state === "READY" ? "success" : detail.state === "ERROR" ? "danger" : detail.state === "UPDATE_AVAILABLE" ? "warning" : "neutral");
  $("offlineStatus").textContent = detail.message || (detail.ready ? "Ready for offline use." : "Offline files are not ready.");
  $("cacheId").textContent = detail.cacheId;
  $("cacheVersion").textContent = detail.version ? "v" + detail.version : "Unknown";
  $("cacheUpdated").textContent = detail.updatedAt ? new Date(detail.updatedAt).toLocaleString() : "Unknown";
  $("update").hidden = detail.state !== "UPDATE_AVAILABLE";
  $("checkOffline").disabled = detail.state === "CACHING" || detail.state === "NOT_SUPPORTED";
  $("prepareOffline").disabled = detail.state === "CACHING" || detail.state === "NOT_SUPPORTED";
});
$("checkOffline").addEventListener("click", () => offline.check());
$("prepareOffline").addEventListener("click", () => offline.prepare());
$("update").addEventListener("click", () => {
  if (busy || bluetooth.device?.gatt?.connected) { message("Disconnect Bluetooth after the current operation before installing the update.", "warning"); return; }
  offline.applyUpdate();
});
$("appVersion").textContent = "AquaCulture Setup " + globalThis.AQUACULTURE_VERSION.version + " · Protocol 2";
offline.check().then((status) => {
  if (!status.ready && status.state !== "NOT_SUPPORTED" && navigator.onLine) offline.prepare();
});
controls();
