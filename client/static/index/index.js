(() => {
  "use strict";

  const $ = (selector, parent = document) => parent.querySelector(selector);
  const $$ = (selector, parent = document) => Array.from(parent.querySelectorAll(selector));
  const BLANK_CAMERA_SRC = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==";

  const state = {
    sessionId: loadSessionId(),
    backendStatus: "Checking",
    activePage: initialPageFromPath(),
    species: [],
    speciesIndex: 0,
    speciesQuery: "",
    socket: null,
    socketUnavailable: false,
    cameraRotation: ([0, 90, 180, 270].includes(Number(localStorage.getItem("ac_camera_rotation"))) ? Number(localStorage.getItem("ac_camera_rotation")) : 0),
    cameraMirrored: localStorage.getItem("ac_camera_mirrored") === "true",
    cameraStreamActive: false,
    streamInfo: null,
    cameraStatus: null,
    cameraStatusTimer: null,
    cameraRetryTimer: null,
    adminToken: localStorage.getItem("aquaculture_admin_token") || "",
    adminSession: loadSavedAdminSession(),
    loggedIn: false,
    sessionExpiryTimer: null,
    authRevision: 0,
    loggingOut: false,
    lastSensorAt: 0,
    modelsReady: false,
    visualJobId: null,
    visualJobRunning: false,
    visualPollTimer: null,
    visualLastFrameKey: "",
    visualLastResultJobId: null,
  };

  document.addEventListener("DOMContentLoaded", init);

  function init() {
    applySavedTheme();
    // One source of tooltip text: native title + custom data-tooltip causes duplication.
    document.querySelectorAll("[data-tooltip][title]").forEach(node => node.removeAttribute("title"));
    updateHeaderSession();
    bindHeader();
    bindHome();
    updateHomeDateTime();
    window.setInterval(updateHomeDateTime, 60_000);
    bindLive();
    bindCameraViewControls();
    bindModals();
    bindManualWaterTools();
    syncAuthUi();
    checkAdminSession();
    window.setInterval(checkAdminSession, 60_000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) checkAdminSession(); });
    window.addEventListener("storage", (event) => {
      if (event.key !== "aquaculture_admin_token") return;
      state.authRevision += 1;
      state.adminToken = localStorage.getItem("aquaculture_admin_token") || "";
      state.loggedIn = false;
      syncAuthUi();
      checkAdminSession();
    });
    loadSpecies();
    loadModelReadiness();
    loadInitialBackendState();
    showPage(state.activePage, { replace: true, scroll: false });
    window.addEventListener("popstate", () => showPage(initialPageFromPath(), { replace: true, scroll: false }));
    connectSocketWhenReady();
    if (new URLSearchParams(window.location.search).get("login") === "settings-required") openDialog("loginModal");
  }

  function bindHeader() {
    const sidebar = $("#headerMenu");
    const hamburger = $("#hamburgerButton");
    const backdrop = $("#menuBackdrop");
    let previousFocus = null;

    const setMenuOpen = (open) => {
      const willOpen = Boolean(open);
      if (willOpen === sidebar?.classList.contains("is-active")) return;
      if (willOpen) previousFocus = document.activeElement;
      sidebar?.classList.toggle("is-active", willOpen);
      backdrop?.classList.toggle("is-active", willOpen);
      hamburger?.setAttribute("aria-expanded", String(willOpen));
      document.body.classList.toggle("ac2-menu-open", willOpen);
      if (willOpen) $("#menuClose")?.focus();
      else if (previousFocus && typeof previousFocus.focus === "function" && window.innerWidth <= 900) previousFocus.focus();
    };
    hamburger?.addEventListener("click", () => setMenuOpen(!sidebar?.classList.contains("is-active")));
    backdrop?.addEventListener("click", () => setMenuOpen(false));
    $("#menuClose")?.addEventListener("click", () => setMenuOpen(false));

    $$('[data-page-target]').forEach((button) => {
      button.addEventListener("click", (event) => {
        if (button.tagName === "A") event.preventDefault();
        const page = button.getAttribute("data-page-target") || "home";
        setMenuOpen(false);
        showPage(page);
      });
    });
    $("#themeButton")?.addEventListener("click", toggleTheme);
    $("#loginButton")?.addEventListener("click", () => {
      setMenuOpen(false);
      syncAuthUi();
      openDialog("loginModal");
    });
    window.addEventListener("resize", () => { if (window.innerWidth > 900) setMenuOpen(false); });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") setMenuOpen(false);
      if (event.key === "Tab" && sidebar?.classList.contains("is-active") && window.innerWidth <= 900) {
        const focusable = $$('a[href], button:not([disabled])', sidebar).filter((node) => node.getClientRects().length);
        if (focusable.length) {
          if (event.shiftKey && document.activeElement === focusable[0]) {event.preventDefault();focusable.at(-1).focus();}
          else if (!event.shiftKey && document.activeElement === focusable.at(-1)) {event.preventDefault();focusable[0].focus();}
        }
      }
    });
  }

  function bindHome() {
    $("#speciesPrev")?.addEventListener("click", () => moveSpecies(-1));
    $("#speciesNext")?.addEventListener("click", () => moveSpecies(1));
    $("#homeSpeciesSearch")?.addEventListener("input", (event) => {
      state.speciesQuery = String(event.target.value || "").trim().toLowerCase();
      state.speciesIndex = 0;
      renderSpecies();
    });
    $("#speciesStage")?.addEventListener("keydown", (event) => {
      if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
        event.preventDefault();
        moveSpecies(event.key === "ArrowRight" ? 1 : -1);
      }
    });
    $("#homeBrowseSpeciesButton")?.addEventListener("click", () => {
      document.getElementById("species-guide")?.scrollIntoView({ behavior: "smooth", block: "center" });
    });
    $("#homeNetworkHelpButton")?.addEventListener("click", () => {
      setText("#homeCurrentAddress", `${window.location.origin}/`);
      openDialog("networkHelpDialog");
    });
    $("#homeCopyLinkButton")?.addEventListener("click", copyCurrentAddress);
  }

  function updateHomeDateTime() {
    const stamp = new Date();
    setText("#homeLocalTime", stamp.toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
    }));
  }

  async function copyCurrentAddress() {
    const address = `${window.location.origin}/`;
    let copied = false;
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(address);
        copied = true;
      } else {
        const field = document.createElement("textarea");
        field.value = address;
        field.style.position = "fixed";
        field.style.opacity = "0";
        document.body.append(field);
        field.select();
        copied = document.execCommand("copy");
        field.remove();
      }
    } catch (_) { /* browsers may restrict clipboard on non-secure origins */ }
    showToast(copied ? "Current page address copied." : `Copy this address: ${address}`, copied ? "success" : "info");
  }

  function bindLive() {
    $("#refreshLiveButton")?.addEventListener("click", loadInitialBackendState);

    $("#visualScanButton")?.addEventListener("click", startVisualScan);
    $("#visualCancelButton")?.addEventListener("click", cancelVisualScan);
    $("#recommendationScanButton")?.addEventListener("click", startRecommendationScan);

    const cameraFeed = $("#cameraFeed");
    if (cameraFeed) {
      cameraFeed.addEventListener("load", () => {
        if (state.cameraStreamActive && state.cameraStatus) renderCameraStatus(state.cameraStatus);
      });
      cameraFeed.addEventListener("error", () => {
        cameraFeed.classList.add("is-unavailable");
        if (!state.cameraStreamActive) return;
        setText("#cameraStatus", "Camera feed interrupted — Retrying");
        window.clearTimeout(state.cameraRetryTimer);
        state.cameraRetryTimer = window.setTimeout(() => {
          if (!state.cameraStreamActive) return;
          const stream = state.streamInfo?.mjpeg_url || cameraFeed.dataset.streamSrc || "/api/camera/stream";
          const url = new URL(stream, window.location.origin);
          url.searchParams.set("reconnect", String(Date.now()));
          cameraFeed.src = url.href;
          refreshCameraStatus();
        }, 5000);
      });
    }
  }

  function bindModals() {

    $$('[data-dialog-close]').forEach((button) => {
      button.addEventListener("click", () => closeDialog(button.getAttribute("data-dialog-close")));
    });

    $("#logoutButton")?.addEventListener("click", logoutAdmin);
    $("#loginModal")?.addEventListener("close", () => $("#loginForm")?.reset());

    $("#loginForm")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (state.loggedIn || state.loggingOut) return;

      const password = $("#adminPassword")?.value || "";
      if (!password.trim()) {
        setLoginMessage("Enter the admin password first.", true);
        showToast("Enter the admin password first.", "warning");
        return;
      }

      const submitButton = $("#loginSubmitButton");
      setBusy(submitButton, true, "Checking");
      setLoginMessage("Checking password…");

      const response = await postJson("/api/admin/password/verify", {
        password,
        session_id: state.sessionId,
      });

      setBusy(submitButton, false, "Sign in");
      $("#loginForm")?.reset();
      if (response.ok && response.admin_token) {
        state.authRevision += 1;
        state.adminToken = response.admin_token;
        state.adminSession = {
          frontend_session_id: response.frontend_session_id || state.sessionId,
          created_at: response.created_at || "",
          expires_at: response.expires_at || "",
          expires_in_seconds: response.expires_in_seconds || 0,
        };
        state.loggedIn = true;
        scheduleSessionExpiry();
        localStorage.setItem("aquaculture_admin_token", state.adminToken);
        localStorage.setItem("aquaculture_admin_session", JSON.stringify(state.adminSession));
        syncAuthUi();
        closeDialog("loginModal");
        showToast("Login successful. Settings, records, and scan controls are available.", "success");
      } else {
        clearAdminSession(false);
        const message = response.message || "Login failed.";
        setLoginMessage(message, true);
        if ($("#loginModal")?.open) $("#adminPassword")?.focus();
        showToast(message, response.status === 429 || response.locked ? "error" : "warning");
      }
    });
  }

  function initialPageFromPath() {
    const path = window.location.pathname.replace(/\/+$/, "") || "/";
    if (["/water-testing", "/water", "/test", "/test.html"].includes(path)) return "water";
    if (["/fish-scanning", "/fish", "/live"].includes(path) || window.location.hash === "#live") return "fish";
    return "home";
  }

  function pageUrl(page) {
    if (page === "water") return "/water-testing";
    if (page === "fish") return "/fish-scanning";
    return "/";
  }

  function showPage(page, options = {}) {
    const targetPage = ["water", "fish"].includes(page) ? page : "home";
    state.activePage = targetPage;
    $$(".page-section").forEach((section) => {
      section.classList.toggle("is-active", section.dataset.page === targetPage);
    });
    $$(".nav-link").forEach((link) => {
      const active = link.getAttribute("data-page-target") === targetPage;
      link.classList.toggle("is-active", active);
      if (active) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
    const label = ({home: "Overview", water: "Water Testing", fish: "Fish Weight Scanning"})[targetPage];
    setText("#pageBreadcrumb", label);
    document.title = `${label} | AquaCulture`;
    if (!options.replace) {
      const nextUrl = pageUrl(targetPage);
      if (`${window.location.pathname}${window.location.hash}` !== nextUrl) {
        window.history.pushState({page: targetPage}, "", nextUrl);
      }
    } else if (window.location.hash === "#live" || window.location.pathname === "/test") {
      window.history.replaceState({page: targetPage}, "", pageUrl(targetPage));
    }
    setCameraStreamActive(targetPage === "fish");
    if (targetPage === "fish") window.requestAnimationFrame(applyCameraView);
    if (targetPage === "water" || targetPage === "fish") loadInitialBackendState();
    if (options.scroll !== false) window.scrollTo({top: 0, behavior: "instant"});
  }

  function applySavedTheme() {
    window.AquaUI?.applyTheme();
    updateThemeButton();
  }

  function toggleTheme() {
    const current = document.documentElement.dataset.theme === "dark" ? "dark" : "light";
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("aquaculture_theme", next);
    updateThemeButton();
  }

  function updateThemeButton() {
    const isDark = document.documentElement.dataset.theme === "dark";
    const button = $("#themeButton");
    setText("#themeIcon", isDark ? "☀" : "◐");
    button?.setAttribute("aria-label", isDark ? "Switch to light theme" : "Switch to dark theme");
    button?.setAttribute("aria-label", isDark ? "Switch to light theme" : "Switch to dark theme");
    if (button) { button.dataset.tooltip = isDark ? "Switch to light theme" : "Switch to dark theme"; button.removeAttribute("title"); }
  }

  function syncAuthUi() {
    $$('[data-admin-only]').forEach((node) => { node.hidden = !state.loggedIn; });
    if ($("#loginSubmitButton")) $("#loginSubmitButton").disabled = state.loggingOut;
    if ($("#logoutButton")) $("#logoutButton").disabled = state.loggingOut;
    const loginButton = $("#loginButton");
    if (loginButton) {
      const loginIcon = loginButton.querySelector(".action-icon");
      const loginLabel = loginButton.querySelector(".action-label");
      const label = state.loggedIn ? "Admin session" : "Login";
      if (loginIcon) loginIcon.textContent = state.loggedIn ? "✓" : "🔒";
      if (loginLabel) loginLabel.textContent = state.loggedIn ? "Admin session" : "Admin Login";
      loginButton.setAttribute("aria-label", label);
      loginButton.setAttribute("title", label);
      loginButton.classList.toggle("btn-primary", !state.loggedIn);
      loginButton.classList.toggle("btn-soft", state.loggedIn);
      loginButton.classList.toggle("is-primary", !state.loggedIn);
      loginButton.classList.toggle("is-light", state.loggedIn);
    }

    const loggedOutPane = $("#loginLoggedOutPane");
    const loggedInPane = $("#loginLoggedInPane");
    if (loggedOutPane) loggedOutPane.hidden = state.loggedIn;
    if (loggedInPane) loggedInPane.hidden = !state.loggedIn;
    setText("#loginModalTitle", state.loggedIn ? "Admin session" : "Admin Login");
    setText("#adminSessionText", `Session #${state.sessionId}`);
    setText("#adminSessionMeta", state.loggedIn ? sessionMetaText() : "Settings, records, and scan controls require login.");

    loginButton?.classList.toggle("is-authenticated", state.loggedIn);
    $$('[data-requires-admin="1"]').forEach((button) => {
      const missingModel = button.id === "visualScanButton" && !state.modelsReady;
      button.disabled = missingModel || (button.id === "visualScanButton" && state.visualJobRunning);
      button.classList.toggle("is-locked", !state.loggedIn);
      button.title = missingModel ? "Trained model files are not installed yet."
        : state.loggedIn ? "" : "Click to sign in before scanning.";
      button.setAttribute("aria-label", missingModel ? "Fish scan unavailable; model files not installed" : button.textContent.trim());
    });
  }

  function sessionMetaText() {
    if (!state.adminSession?.expires_at) return "Settings, records, and scan controls are available on this browser.";
    return `Settings, records, and scan controls are available. Expires: ${state.adminSession.expires_at}`;
  }

  function loadSessionId() {
    const existing = localStorage.getItem("aquaculture_session_id");
    if (existing && /^S-\d{3}$/.test(existing)) return existing;
    const fresh = `S-${String(Math.floor(1 + Math.random() * 999)).padStart(3, "0")}`;
    localStorage.setItem("aquaculture_session_id", fresh);
    return fresh;
  }


  function loadSavedAdminSession() {
    try {
      const saved = localStorage.getItem("aquaculture_admin_session");
      return saved ? JSON.parse(saved) : null;
    } catch (_) {
      return null;
    }
  }

  async function checkAdminSession() {
    if (state.loggingOut) return;
    const revision = state.authRevision;
    const response = await getJson("/api/admin/session");
    if (revision !== state.authRevision || state.loggingOut) return;
    if (response.ok && response.session_active) {
      state.loggedIn = true;
      state.adminSession = {
        frontend_session_id: response.frontend_session_id || state.sessionId,
        created_at: response.created_at || "",
        expires_at: response.expires_at || "",
        expires_in_seconds: response.expires_in_seconds || 0,
      };
      localStorage.setItem("aquaculture_admin_session", JSON.stringify(state.adminSession));
      scheduleSessionExpiry();
    } else if (response.ok || response.status === 401) {
      clearAdminSession(false);
    } else {
      state.loggedIn = false;
    }
    syncAuthUi();
    if (state.loggedIn && !state.visualPollTimer) void refreshVisualStatus();
  }

  function scheduleSessionExpiry() {
    window.clearTimeout(state.sessionExpiryTimer);
    const remaining = Number(state.adminSession?.expires_in_seconds);
    if (!Number.isFinite(remaining)) return;
    state.sessionExpiryTimer = window.setTimeout(() => {
      clearAdminSession(false);
      syncAuthUi();
    }, Math.max(0, remaining * 1000));
  }

  function updateHeaderSession() {
    setText("#sessionPill", `Session #${state.sessionId} · ${state.backendStatus}`);
    setText("#footerConnectionStatus", `${state.backendStatus} · Session #${state.sessionId}`);
    const label = state.backendStatus === "Online" || state.backendStatus === "Live updates"
      ? "System reachable"
      : state.backendStatus === "Checking" ? "Checking system connection…"
      : state.backendStatus === "Reconnecting" ? "Reconnecting to live updates…"
      : state.backendStatus === "Offline" ? "System offline" : "System reachable · limited live updates";
    setText("#homeConnectionLabel", label);
    const dot = $("#homeConnectionDot");
    if (dot) dot.dataset.status = state.backendStatus === "Offline" ? "offline"
      : state.backendStatus === "Online" || state.backendStatus === "Live updates" ? "online" : "checking";
  }

  async function loadInitialBackendState() {
    const health = await getJson("/api/health");
    if (health.ok) {
      state.backendStatus = state.socket?.connected ? "Live updates" : state.socketUnavailable ? "Limited updates" : "Online";
      updateHeaderSession();
      renderCameraStatus(health.camera || {});
      if (health.sensors?.latest) renderSensorReading(health.sensors.latest, { stale: !health.sensors.fresh });
    } else {
      state.backendStatus = "Offline";
      updateHeaderSession();
    }

    const latest = await getJson("/api/sensors/latest?allow_stale=1");
    if (latest.ok && latest.reading) renderSensorReading(latest.reading, { stale: Boolean(latest.stale) });

    const distance = await getJson("/api/sensors/ultrasonic");
    renderDistance(distance);

    const stream = await getJson("/api/stream/info");
    if (stream.ok) renderStreamInfo(stream);
  }

  function currentSpeciesList() {
    return state.speciesQuery
      ? state.species.filter((species) => [species.name, species.scientific_name, ...(species.aliases || [])].some((name) => String(name || "").toLowerCase().includes(state.speciesQuery)))
      : state.species;
  }

  async function loadSpecies() {
    const [response, referenceResponse] = await Promise.all([
      getJson("/api/species?include_image=0"), getJson("/api/species/reference"),
    ]);
    const species = response.ok && Array.isArray(response.species) ? [...response.species] : [];
    const references = referenceResponse.ok && Array.isArray(referenceResponse.species) ? referenceResponse.species : [];
    const normalized = (value) => String(value || "").trim().toLowerCase();
    const usedReferences = new Set();
    const merged = species.map((item) => {
      const reference = references.find((entry) => [entry.common_name, entry.scientific_name, `${entry.common_name} (${entry.scientific_name})`, ...(entry.aliases || [])]
        .some((name) => normalized(name) === normalized(item.name)));
      if (reference) usedReferences.add(reference.id);
      return { ...reference, ...item, reference_only: false };
    });
    merged.push(...references.filter((item) => !usedReferences.has(item.id)).map((item) => ({ ...item, reference_only: true })));
    // Feature one real, image-backed fish from the existing database first.
    // The remaining entries retain their original server-supplied ordering.
    const featured = merged.findIndex((item) =>
      String(item.name || "").trim().toLowerCase() === "tilapia" && item.has_image === true);
    if (featured > 0) merged.unshift(...merged.splice(featured, 1));
    state.species = merged;
    state.speciesIndex = 0;
    setText("#speciesCounter", species.length ? `${species.length} saved` : "Unavailable");
    renderSpecies();
  }

  function renderSpecies() {
    const stage = $("#speciesStage");
    const dots = $("#speciesDots");
    if (!stage || !dots) return;

    const items = currentSpeciesList();
    setText("#speciesCounter", state.speciesQuery ? `${items.length} matching` : state.species.length ? `${state.species.length} saved` : "Unavailable");
    if (!items.length) {
      stage.innerHTML = `<div class="home-species-loading">${state.speciesQuery
        ? "No species match your search. Try another name."
        : "Species are unavailable. Reconnect to the server to load your saved guide."}</div>`;
      dots.innerHTML = "";
      setText("#speciesPosition", "0 / 0");
      $("#speciesPrev")?.setAttribute("disabled", "");
      $("#speciesNext")?.setAttribute("disabled", "");
      return;
    }

    const item = items[state.speciesIndex] || items[0];
    const imageSrc = speciesImageSource(item);
    stage.innerHTML = `
      <article class="species-card">
        <div class="species-image-wrap">
          <img class="previewable-image" data-image-preview="1" src="${escapeAttr(imageSrc)}" alt="${escapeAttr(item.name || "Species")}" onerror="this.onerror=null;this.src='/static/images/fallback-species.svg'">
        </div>
        <div>
          <div class="species-name">${escapeHtml(item.name || "Unnamed species")}</div>
          ${item.scientific_name ? `<p class="species-scientific"><em>${escapeHtml(item.scientific_name)}</em></p>` : ""}
          ${item.reference_only ? `
            <span class="tag is-info is-light">Reference species</span>
            <p class="home-guide-note">${escapeHtml(item.habitat || "")} · ${escapeHtml(item.catalog_scope || "")}</p>
            <p class="home-guide-note">Validated water ranges are needed before this species can be scored.</p>
            ${speciesSourceLink(item)}` : `<div class="range-grid" aria-label="Stored species water ranges">
            ${rangeItem("pH", item.ph)}
            ${rangeItem("Temperature", item.temperature)}
            ${rangeItem("Ammonia*", item.ammonia)}
            ${rangeItem("Oxygen", item.oxygen)}
            ${rangeItem("Turbidity", item.turbidity)}
          </div>
          <p class="home-guide-note">*Species ammonia ranges concern water chemistry, not MQ-137 gas measurements. Existing rules require scientific validation.</p>`}
        </div>
      </article>
    `;
    // Display a compact five-dot window, not 100+ dots for large databases.
    const start = Math.max(0, Math.min(state.speciesIndex - 2, items.length - 5));
    const end = Math.min(items.length, start + 5);
    dots.innerHTML = Array.from({ length: end - start }, (_, index) =>
      `<span class="dot ${start + index === state.speciesIndex ? "is-active" : ""}"></span>`
    ).join("");
    setText("#speciesPosition", `${state.speciesIndex + 1} / ${items.length}`);
    $("#speciesPrev")?.removeAttribute("disabled");
    $("#speciesNext")?.removeAttribute("disabled");
  }

  function speciesImageSource(item) {
    if (item.reference_only) return "/static/images/fallback-species.svg";
    if (item.image_base64) return `data:image/jpeg;base64,${item.image_base64}`;
    if (item.image_url) return item.image_url;
    if (item.has_image === true && item.id) return `/api/species/${item.id}/image`;
    return "/static/images/fallback-species.svg";
  }

  function speciesSourceLink(item) {
    try {
      const source = new URL(item.source_url);
      if (!["https:", "http:"].includes(source.protocol)) return "";
      return `<a class="home-guide-note" href="${escapeAttr(source.href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.source_title || "Research source")} ↗</a>`;
    } catch (_) { return ""; }
  }

  function rangeItem(label, value) {
    return `<div class="range-item"><span>${escapeHtml(label)}</span><strong>${escapeHtml(cleanRange(value))}</strong></div>`;
  }

  function cleanRange(value) {
    if (value === null || value === undefined || value === "") return "—";
    let normalized = value;
    if (typeof value === "string" && value.trim().startsWith("[")) {
      try { normalized = JSON.parse(value); } catch (_) { /* keep original stored text */ }
    }
    // Stored species rules commonly use [minimum, maximum, weighting].
    // Display only the usable sensor range here; weights belong in RBS details.
    if (Array.isArray(normalized) && normalized.length >= 2) {
      const low = Number(normalized[0]);
      const high = Number(normalized[1]);
      if (Number.isFinite(low) && Number.isFinite(high)) return `${formatNumber(low)} – ${formatNumber(high)}`;
      return normalized.slice(0, 2).join(" – ");
    }
    if (typeof normalized === "object") return JSON.stringify(normalized);
    return String(normalized);
  }

  function moveSpecies(direction) {
    const length = currentSpeciesList().length;
    if (!length) return;
    state.speciesIndex = (state.speciesIndex + direction + length) % length;
    renderSpecies();
  }

  function connectSocketWhenReady() {
    const started = Date.now();
    const waitForSocketClient = () => {
      if (typeof window.io === "function") {
        connectSocket();
        return;
      }
      if (Date.now() - started < 5000) {
        window.setTimeout(waitForSocketClient, 150);
        return;
      }
      // Offline/CDN-blocked networks must not be reported as live Socket.IO connections.
      state.socketUnavailable = true;
      if (state.backendStatus === "Online") state.backendStatus = "Limited updates";
      updateHeaderSession();
      window.setInterval(() => {
        if (document.visibilityState !== "hidden" && !state.socket?.connected) loadInitialBackendState();
      }, 12_000);
    };
    waitForSocketClient();
  }

  function connectSocket() {
    try {
      // Polling avoids Werkzeug's intermittent WebSocket upgrade 500 on Windows.
      // Socket.IO event names/payloads are unchanged; WebSocket can be re-enabled
      // later behind a tested production server by setting this opt-in flag.
      const socket = window.io({
        transports: window.AQUACULTURE_ENABLE_WEBSOCKET === true ? ["polling", "websocket"] : ["polling"],
        upgrade: window.AQUACULTURE_ENABLE_WEBSOCKET === true,
        reconnection: true,
        reconnectionAttempts: Infinity,
      });
      state.socket = socket;

      socket.on("connect", () => {
        state.socketUnavailable = false;
        state.backendStatus = "Live updates";
        updateHeaderSession();
        socket.emit("sensors:latest", { session_id: state.sessionId });
      });

      socket.on("disconnect", () => {
        state.backendStatus = "Reconnecting";
        updateHeaderSession();
      });

      socket.on("system_state", (data) => {
        state.backendStatus = "Live updates";
        updateHeaderSession();
        if (data?.sensors?.latest) renderSensorReading(data.sensors.latest, { stale: !data.sensors.fresh });
        if (data?.camera) renderCameraStatus(data.camera);
        if (data?.stream) renderStreamInfo(data.stream);
      });

      socket.on("sensor_latest", (data) => renderSensorReading(data?.reading || data));
      socket.on("sensor_status", (data) => {
        if (data?.latest) renderSensorReading(data.latest, { stale: !data.fresh });
      });
      socket.on("camera_status", renderCameraStatus);
      socket.on("stream_status", renderStreamInfo);
      socket.on("prediction_event", renderPredictionEvent);
      socket.on("recommendation_event", renderRecommendationEvent);
      socket.on("prediction_status", (data) => renderStatusChip("#visualStatusChip", data));
      socket.on("recommendation_status", (data) => renderStatusChip("#recommendationStatusChip", data));
      socket.on("scan_status", renderScanStatus);
      socket.on("alert_status", (data) => { if (data?.sent) showToast(data.message || "Alert notification sent.", "success"); });
    } catch (error) {
      state.backendStatus = "Limited updates";
      updateHeaderSession();
    }
  }

  function renderSensorReading(reading, options = {}) {
    if (!reading || typeof reading !== "object") return;
    state.lastSensorAt = Date.now();
    const measurements = [
      { value: pick(reading, ["ph", "pH"]), suffix: "", field: "Ph", liveId: "#sensorPh" },
      { value: pick(reading, ["temperature", "temperature_c", "temp"]), suffix: " °C", field: "Temperature", liveId: "#sensorTemperature" },
      { value: pick(reading, ["turbidity", "turbidity_ntu", "ntu"]), suffix: " NTU", field: "Turbidity", liveId: "#sensorTurbidity" },
      { value: pick(reading, ["ammonia_gas_ppm", "ammonia"]), suffix: " ppm gas", field: "MQ-137 ammonia gas", liveId: "#sensorAmmonia" },
      { value: pick(reading, ["oxygen", "oxygen_mg_l", "dissolved_oxygen"]), suffix: " mg/L", field: "Oxygen", liveId: "#sensorOxygen" },
    ];
    const received = Number(reading.received_epoch);
    const inferredStale = Number.isFinite(received) && received > 0 && ((Date.now() / 1000) - received > 30);
    const stale = Boolean(options.stale || inferredStale);
    let available = 0;
    for (const item of measurements) {
      setText(item.liveId, formatValue(item.value, item.suffix));
      const valid = item.value !== null && item.value !== undefined && item.value !== "" && Number.isFinite(Number(item.value));
      const valueEl = $(`#home${item.field}Preview`);
      const statusEl = $(`#home${item.field}State`);
      if (valueEl) valueEl.textContent = valid ? formatValue(item.value, item.suffix) : "—";
      if (statusEl) {
        statusEl.textContent = !valid ? "No data" : stale ? "Stale" : "Received";
        statusEl.dataset.available = String(valid && !stale);
      }
      if (valid && !stale) available += 1;
    }
    setText("#homeReadingsCaption", stale
      ? "Showing last known readings. They may no longer reflect current water conditions."
      : available > 0
        ? `${available} of 5 sensor readings received. Values are displayed as reported; calibration and stability are checked during dedicated water scans.`
        : "Waiting for sensor data. Dashboard features remain available without connected hardware.");

    const distance = pick(reading, ["distance_cm", "distance", "ultrasonic", "ultrasonic_cm"]);
    if (distance !== undefined && distance !== null) setText("#distanceBadge", `Distance ${formatNumber(distance)} cm`);
  }

  function renderDistance(data) {
    if (data?.ok && data.distance_cm !== undefined && data.distance_cm !== null) {
      setText("#distanceBadge", `Distance ${formatNumber(data.distance_cm)} cm`);
    } else {
      setText("#distanceBadge", "Distance --");
    }
  }

  async function loadModelReadiness() {
    const result = await getJson("/api/models/status");
    state.modelsReady = Boolean(result.ok && result.ready);
    const status = $("#modelReadiness");
    if (status) {
      status.dataset.ready = String(state.modelsReady);
      const label = status.querySelector("span:last-child");
      if (label) label.textContent = state.modelsReady
        ? "Model files detected; compatibility must be verified on this device."
        : "Model files are not installed in this phase.";
    }
    syncAuthUi();
  }

  function renderCameraStatus(data) {
    if (!data || typeof data !== "object") return;
    state.cameraStatus = data;
    if (!state.cameraStreamActive) return;
    const active = Boolean(data.connected && data.fresh);
    const source = data.source === "csi" ? "CSI" : data.source === "usb" ? "USB Webcam" : data.source_label || "Camera";
    const message = active ? `Camera: ${source} — Active`
      : data.running ? "Camera: Unavailable — Retrying" : data.message || "Camera stopped";
    setText("#cameraStatus", message);
    const badge = $("#cameraStatus");
    if (badge) badge.title = [data.device, data.last_error].filter(Boolean).join(" · ");
    $("#cameraFeed")?.classList.toggle("is-unavailable", !active);
    if (data.fps || data.current_fps || data.last_fps) {
      setText("#fpsBadge", `FPS ${formatNumber(data.fps || data.current_fps || data.last_fps)}`);
    }
  }

  async function refreshCameraStatus() {
    if (!state.cameraStreamActive) return;
    const status = await getJson("/api/camera/status");
    if (state.cameraStreamActive && typeof status.connected === "boolean") renderCameraStatus(status);
  }

  function renderStreamInfo(data) {
    if (!data || typeof data !== "object") return;
    state.streamInfo = data;
    if (state.activePage !== "fish") {
      setCameraStreamActive(false);
      return;
    }
    setCameraStreamActive(true);
  }

  function setCameraStreamActive(active) {
    const cameraFeed = $("#cameraFeed");
    if (!cameraFeed) return;

    const wasActive = state.cameraStreamActive;
    state.cameraStreamActive = Boolean(active);
    const defaultStream = cameraFeed.dataset.streamSrc || "/api/camera/stream";

    if (!active) {
      window.clearInterval(state.cameraStatusTimer);
      state.cameraStatusTimer = null;
      window.clearTimeout(state.cameraRetryTimer);
      if (cameraFeed.getAttribute("src") !== BLANK_CAMERA_SRC) {
        cameraFeed.setAttribute("src", BLANK_CAMERA_SRC);
      }
      setText("#cameraStatus", "Server camera paused");
      return;
    }
    if (!wasActive) {
      refreshCameraStatus();
      state.cameraStatusTimer = window.setInterval(refreshCameraStatus, 5000);
    }

    const data = state.streamInfo || {};
    const mjpeg = data.mjpeg_url || data.mjpeg_fallback_url || data.mjpeg || defaultStream;
    if (cameraFeed.getAttribute("src") !== mjpeg) {
      cameraFeed.setAttribute("src", mjpeg);
    }
  }

  // The HTTP request only starts a background job. Poll the job status while
  // Pi NCNN inference runs: do not abort after the 3/5/10-second capture time.
  async function startVisualScan() {
    if (!state.modelsReady) { showToast("Fish model weights have not been installed.", "warning"); return; }
    if (!requireAdminForScan() || state.visualJobRunning) return;
    const duration = numberInput("#visualDuration", 3, 1, 10);
    const images = numberInput("#visualImages", 5, 1, 10);
    const button = $("#visualScanButton");
    setBusy(button, true, "Starting...");
    state.visualLastFrameKey = "";
    state.visualLastResultJobId = null;
    $("#visualScanProgress").hidden = false;
    setHtml("#visualResult", `<div class="empty-state">Recording ${duration} seconds of camera video. AI predictions will follow.</div>`);

    const response = await postJson("/api/prediction/start", {
      session_id: state.sessionId,
      duration_seconds: duration,
      top: images,
      target_fps: Math.min(10, Math.max(4, Math.ceil((images * 2) / duration))),
    }, { timeoutMs: 15000 });

    setBusy(button, false, "Start fish scan ↗");
    if (!response.ok || !response.accepted) {
      setText("#visualStatusChip", "Unavailable");
      setHtml("#visualResult", errorBlock(response.message || "Scan could not start."));
      // An HTTP timeout can occur after the backend has accepted the job.
      // Check status before treating it as a lost scan.
      void refreshVisualStatus();
      return;
    }
    state.visualJobId = response.job_id;
    state.visualJobRunning = true;
    syncAuthUi();
    $("#visualCancelButton").hidden = false;
    renderVisualStatus(response.status || { running: true, stage: "recording", progress: 0 });
    scheduleVisualPoll(0);
  }

  async function cancelVisualScan() {
    if (!requireAdminForScan() || !state.visualJobRunning) return;
    const cancel = $("#visualCancelButton");
    if (cancel) { cancel.disabled = true; cancel.textContent = "Cancelling..."; }
    const response = await postJson("/api/prediction/cancel", {}, { timeoutMs: 10000 });
    if (!response.ok) showToast(response.message || "Could not cancel scan.", "warning");
    scheduleVisualPoll(0);
  }

  function scheduleVisualPoll(delayMs = 850) {
    window.clearTimeout(state.visualPollTimer);
    state.visualPollTimer = window.setTimeout(() => {
      state.visualPollTimer = null;
      void refreshVisualStatus();
    }, delayMs);
  }

  async function refreshVisualStatus() {
    if (!state.loggedIn) return;
    const response = await getJson("/api/prediction/status");
    if (!response.ok) {
      if (state.visualJobRunning) {
        setText("#visualStageMessage", "Connection interrupted; waiting to resume updates...");
        scheduleVisualPoll(2000);
      }
      return;
    }
    renderVisualStatus(response);
    if (response.running) scheduleVisualPoll(850);
  }

  function renderVisualStatus(status) {
    if (!status || typeof status !== "object") return;
    if (status.job_id && state.visualJobId !== status.job_id) {
      state.visualJobId = status.job_id;
      state.visualLastFrameKey = "";
      state.visualLastResultJobId = null;
    }
    const progressPanel = $("#visualScanProgress");
    if (progressPanel && (status.running || (status.stage && status.stage !== "idle"))) progressPanel.hidden = false;
    const stage = status.stage || "idle";
    const percentage = Math.max(0, Math.min(100, Number(status.progress) || 0));
    const stages = ["recording", "selecting", "predicting", "complete"];
    const activeIndex = stage === "failed" || stage === "cancelled" ? 3 : stages.indexOf(stage);
    $$('[data-scan-stage]').forEach((item, index) => {
      item.classList.toggle("is-active", index === activeIndex);
      item.classList.toggle("is-complete", index < activeIndex || (stage === "complete" && index <= activeIndex));
    });
    setText("#visualStageMessage", status.message || "Working...");
    setText("#visualProgressText", `${percentage}%`);
    const bar = $("#visualProgressBar");
    if (bar) bar.setAttribute("aria-valuenow", String(percentage));
    const fill = $("#visualProgressFill");
    if (fill) fill.style.width = `${percentage}%`;
    const total = Number(status.frames_total) || 0;
    const done = Number(status.frames_done) || 0;
    const detail = stage === "recording"
      ? `Recorded ${Number(status.recorded_seconds || 0).toFixed(1)} / ${status.duration_seconds || "?"} seconds · ${status.snapshots_captured || 0} frames captured`
      : stage === "selecting" ? "Ranking frames by sharpness and exposure. Rejected candidates fade away."
      : stage === "predicting" ? `AI analyzed ${done} / ${total} selected frames. Pi 4B models may take 20–30+ seconds per frame.`
      : stage === "complete" ? "Results ready. The winning frame is highlighted in green."
      : stage === "cancelled" ? "The scan has been cancelled."
      : stage === "failed" ? "Analysis failed. Please review the error below."
      : "Select a capture duration to begin.";
    setText("#visualProgressDetail", detail);
    const frames = Array.isArray(status.frames) ? status.frames : [];
    const frameKey = JSON.stringify(frames.map(f => [f.image_id, f.status, f.object_count, f.confidence, Boolean(f.preview)]));
    if (frameKey !== state.visualLastFrameKey) {
      state.visualLastFrameKey = frameKey;
      setHtml("#visualFrameGrid", frames.map((frame, index) => {
        const discarded = frame.status === "discarded";
        const best = frame.status === "best";
        const label = discarded ? "Discarded" : best ? "Best frame" : frame.status === "done" ? `${Number(frame.object_count) || 0} objects` : frame.status === "failed" ? "Failed" : "Queued";
        return `<div class="ac-scan-frame${frame.selected ? " is-shortlisted" : ""}${discarded ? " is-discarded" : ""}${best ? " is-best" : ""}${frame.status === "failed" ? " is-failed" : ""}">
          ${frame.preview ? `<img alt="Recorded frame ${index + 1}" src="${escapeAttr(frame.preview)}">` : `<div class="ac-scan-frame-empty">Frame ${index + 1}</div>`}
          <span class="ac-scan-frame-label">${discarded ? "⌫" : best ? "✓" : "◦"} ${escapeHtml(label)}</span>
          ${frame.confidence !== null && frame.confidence !== undefined ? `<small>${Math.round(Number(frame.confidence) * 100)}% confidence</small>` : ""}
        </div>`;
      }).join(""));
    }
    state.visualJobRunning = Boolean(status.running);
    syncAuthUi();
    const cancel = $("#visualCancelButton");
    if (cancel) {
      cancel.hidden = !state.visualJobRunning;
      if (!status.cancel_requested) { cancel.disabled = false; cancel.textContent = "Cancel scan"; }
    }
    setText("#visualStatusChip", status.running ? `${percentage}%` : stage === "complete" ? "Completed" : stage === "failed" ? "Failed" : stage === "cancelled" ? "Cancelled" : "Idle");
    if (!status.running && status.last_result && state.visualLastResultJobId !== status.job_id) {
      state.visualLastResultJobId = status.job_id;
      if (status.last_result.ok === false) {
        setHtml("#visualResult", errorBlock(status.last_result.message || status.last_result.error || "Visual scan failed."));
      } else {
        renderPredictionResult(status.last_result);
      }
    }
  }

  async function startRecommendationScan() {
    if (!requireAdminForScan()) return;
    const button = $("#recommendationScanButton");
    const duration = numberInput("#recommendationDuration", 5, 3, 5);
    const top = numberInput("#recommendationTop", 3, 1, 10);

    setBusy(button, true, "SCANNING");
    setText("#recommendationStatusChip", "Scanning");
    setHtml("#recommendationResult", `<div class="empty-state">Reading sensors for about ${duration} second(s)…</div>`);

    const response = await postJson("/api/water/scan", {
      session_id: state.sessionId,
      admin_token: state.adminToken,
      duration_seconds: duration,
      top,
    }, { timeoutMs: duration * 1000 + 1500 });

    setBusy(button, false, "Start water scan ↗");
    const result = response.result || response;
    if (response.ok || result.ok) {
      renderRecommendationResult(result);
      setText("#recommendationStatusChip", "Completed");
    } else {
      setText("#recommendationStatusChip", "Failed");
      setHtml("#recommendationResult", errorBlock(response.message || result.message || "Recommendation scan failed safely."));
    }
  }

  function renderScanStatus(data) {
    if (!data || typeof data !== "object") return;
    const bar = $("#scanActivityBar");
    if (!bar) return;
    const kind = data.kind === "recommendation" ? "Water recommendation" : "Visual size detection";
    const startedBy = data.started_by?.frontend_session_id || "another device";
    const mine = String(startedBy) === String(state.sessionId);
    if (data.running) {
      bar.hidden = false;
      setText("#scanActivityTitle", `${kind} is running`);
      setText("#scanActivityText", mine ? "This browser started the scan." : `Started by Session #${startedBy}. Please wait for the result.`);
      return;
    }
    setText("#scanActivityTitle", `${kind} finished`);
    setText("#scanActivityText", data.message || "Scan finished.");
    window.setTimeout(() => {
      if (bar) bar.hidden = true;
    }, 4500);
  }

  function renderPredictionEvent(event) {
    if (!event || (state.visualJobId && event.job_id && event.job_id !== state.visualJobId)) return;
    // Keep fast socket hints, but use the authenticated status endpoint as the
    // source of truth for thumbnails and per-frame progress.
    if (event.message && state.visualJobRunning) setText("#visualStageMessage", event.message);
    if (state.visualJobRunning) scheduleVisualPoll(100);
  }

  function renderRecommendationEvent(event) {
    if (!event) return;
    if (event.message) setText("#recommendationStatusChip", `${event.progress || 0}%`);
    if (event.result) renderRecommendationResult(event.result);
  }

  function renderStatusChip(selector, data) {
    if (!data) return;
    if (data.running) setText(selector, `${data.progress || 0}%`);
    else if (data.message) setText(selector, data.message.length > 20 ? "Idle" : data.message);
  }

  function renderPredictionResult(result) {
    const objects = Array.isArray(result.objects) ? result.objects : [];
    const images = Array.isArray(result.images) ? result.images : [];
    const warnings = Array.isArray(result.warnings) ? result.warnings : [];
    const bestId = result.best_frame_id;
    const bestImage = images.find(image => image.image_id === bestId) || images[0];
    const chosenImage = bestImage?.boxed_image_data_url || bestImage?.raw_image_data_url || "";
    const imageHtml = chosenImage ? `<img class="result-image previewable-image" data-image-preview="1" alt="Best frame prediction result" src="${escapeAttr(chosenImage)}">` : "";
    const frameTiles = images.length ? `<div class="ac-result-frames">${images.map(image => {
      const src = image.boxed_image_data_url || image.raw_image_data_url;
      const best = image.image_id === bestId;
      return `<div class="ac-result-frame${best ? " is-best" : ""}">
        ${src ? `<img src="${escapeAttr(src)}" alt="Analyzed frame ${escapeAttr(image.image_id || "")}">` : ""}
        <span>${best ? "✓ Best frame" : escapeHtml(image.image_id || "Frame")} · ${Number(image.object_count) || 0} detected</span>
      </div>`;
    }).join("")}</div>` : "";
    const objectCards = objects.slice(0, 8).map((object, index) => `
      <article class="result-card">
        <h3>${escapeHtml(object.class || object.class_name || `Object ${index + 1}`)}</h3>
        <div class="meta-grid">
          ${metaItem("Confidence", percentOrNumber(object.confidence))}
          ${metaItem("Length", cm(object.estimated_length_cm))}
          ${metaItem("Width", cm(object.estimated_width_cm))}
          ${metaItem("Distance", cm(object.distance_cm))}
          ${metaItem("Weight", grams(object.estimated_weight_g))}
        </div>
      </article>
    `).join("");
    const warningHtml = warnings.length ? `<p class="is-warn">${escapeHtml(warnings[0])}</p>` : "";
    const emptyMessage = result.message || "No aquatic target was detected in the analyzed frames.";
    const downloadHtml = result.download_available && result.prediction_id
      ? `<a class="ac2-button ac2-button-outline" href="/api/prediction/${encodeURIComponent(result.prediction_id)}/download">Download scan archive</a>` : "";
    setHtml("#visualResult", `
      ${imageHtml}
      ${warningHtml}
      ${frameTiles}
      ${objectCards || `<div class="empty-state">${escapeHtml(emptyMessage)}</div>`}
      ${downloadHtml}
    `);
  }

  function renderRecommendationResult(result) {
    renderRecommendationCardsInto("#recommendationResult", result);
  }

  function readableDetails(item) {
    if (item.environment && item.environment.length) return `Environment rule: ${item.environment.join(", ")}`;
    if (item.regulation && item.regulation.length) return `Regulation rule: ${item.regulation.join(", ")}`;
    return "Matched using the currently available water sensor readings.";
  }


  function bindManualWaterTools() {
    $("#testRecommendationScanButton")?.addEventListener("click", scanTestRecommendation);
    $("#testClearReadingsButton")?.addEventListener("click", clearTestReadings);
  }

  async function scanTestRecommendation() {
    const button = $("#testRecommendationScanButton");
    const values = {
      temperature: numberOrNull("#testTemperature"),
      ph: numberOrNull("#testPh"),
      oxygen: numberOrNull("#testOxygen"),
      turbidity: numberOrNull("#testTurbidity"),
      ammonia: null, // MQ-137 measures gas, not dissolved ammonia. Await validated gas-risk logic.
    };
    const available = Object.values(values).filter((value) => value !== null).length;
    if (available < 3) {
      showToast("Enter at least 3 valid readings for comparison.", "warning");
      setHtml("#testRecommendationResult", `<div class="empty-state">Enter at least 3 readings before testing recommendation.</div>`);
      return;
    }

    setBusy(button, true, "SCANNING");
    setHtml("#testRecommendationResult", `<div class="empty-state">Comparing readings with saved species rules…</div>`);
    const response = await postJson("/api/test/recommendation", {
      ...values,
      top: numberInput("#testRecommendationTop", 3, 3, 10),
    });
    setBusy(button, false, "Compare readings");

    const result = normalizeRecommendationPayload(response);
    if (response.ok || result.ok) {
      renderRecommendationCardsInto("#testRecommendationResult", result);
      showToast("Recommendation test completed.", "success");
    } else {
      setHtml("#testRecommendationResult", errorBlock(response.message || result.message || "Recommendation test failed safely."));
      showToast(response.message || result.message || "Recommendation test failed safely.", "error");
    }
  }

  function clearTestReadings() {
    ["#testTemperature", "#testPh", "#testOxygen", "#testTurbidity", "#testAmmonia"].forEach((selector) => {
      const input = $(selector);
      if (input) input.value = "";
    });
    setHtml("#testRecommendationResult", `<div class="empty-state">Manual recommendation result will appear here.</div>`);
  }

  function renderRecommendationCardsInto(selector, raw) {
    const target = $(selector);
    if (!target) return;
    const result = normalizeRecommendationPayload(raw);
    const suggestions = Array.isArray(result.suggestions) ? result.suggestions : [];
    if (!suggestions.length) {
      target.innerHTML = `<div class="empty-state">${escapeHtml(result.message || "No species suggestion was returned.")}</div>`;
      return;
    }

    const stability = result.stability_warning
      ? `<p class="is-warn">${escapeHtml(result.stability_warning)}</p>` : "";
    const coverage = result.operational_sensor_coverage
      ? `<p class="accuracy-note">Operational sensors: ${escapeHtml(result.operational_sensor_coverage)}. Water parameters: ${escapeHtml(String(result.water_parameter_count ?? 0))}/4.</p>` : "";
    const note = (result.accuracy_note ? `<p class="accuracy-note">${escapeHtml(result.accuracy_note)}</p>` : "") + coverage + stability;
    target.innerHTML = note + suggestions.map((item, index) => {
      const score = clampNumber(item.percentage ?? item.fit_percentage ?? item.overall_percentage ?? 0, -100, 100);
      const shownScore = Math.max(0, Math.min(100, score));
      const confidence = clampNumber(item.confidence_percentage ?? 0, 0, 100);
      const scoreLabel = score < 0 ? `${formatNumber(score)}%` : `${formatNumber(shownScore)}%`;
      const imageSrc = speciesImageSource(item);
      const criticalWarnings = Array.isArray(item.critical_warnings) ? item.critical_warnings : [];
      const parameterWarnings = Array.isArray(item.parameter_warnings) ? item.parameter_warnings : [];
      const safetyLabel = criticalWarnings.length ? "CRITICAL WARNING" : "Not verified safe";
      const warningHtml = [...criticalWarnings, ...parameterWarnings].map((warning) =>
        `<p class="is-warn">${escapeHtml(warning)}</p>`
      ).join("");
      const paramLabels = { temperature: "Temperature", ph: "pH", oxygen: "Dissolved oxygen", turbidity: "Turbidity" };
      const scoreBreakdown = Object.entries(item.matches || {})
        .filter(([key]) => Object.hasOwn(paramLabels, key))
        .map(([key, detail]) => {
          const compared = detail.rule_valid && detail.reading !== null && detail.reading !== undefined;
          const scoreText = compared ? `${formatNumber(detail.suitability_percentage)}%` : "Not compared";
          const rangeText = detail.rule_valid ? `${formatNumber(detail.min)}–${formatNumber(detail.max)}` : "No valid rule";
          return `<tr><td>${escapeHtml(paramLabels[key])}</td><td>${escapeHtml(rangeText)}</td>` +
            `<td>${compared ? escapeHtml(formatNumber(detail.reading)) : "—"}</td>` +
            `<td>${escapeHtml(String(scoreText))}</td><td>${escapeHtml(formatNumber(detail.rule_weight || 0))}</td></tr>`;
        }).join("");
      const detailHtml = scoreBreakdown ? `<details class="ac2-details ac2-rbs-details"><summary>How this RBS was calculated</summary>` +
        `<div style="overflow-x:auto"><table class="table is-fullwidth is-striped"><thead><tr><th>Water parameter</th><th>Range</th><th>Reading</th><th>Suitability</th><th>Weight</th></tr></thead>` +
        `<tbody>${scoreBreakdown}</tbody></table></div>` +
        `<p class="accuracy-note">RBS is the weight-normalized mean of valid comparable parameters. Missing values are excluded; MQ-137 gas has no water score or numerical risk penalty.</p></details>` : "";
      return `
        <article class="recommendation-card has-image media">
          <figure class="recommendation-image media-left">
            <img class="previewable-image" data-image-preview="1" src="${escapeAttr(imageSrc)}" alt="${escapeAttr(item.name || "Suggested species")}" onerror="this.src='/static/images/fallback-species.svg'">
          </figure>
          <div class="media-content recommendation-card-body">
            <div class="recommendation-card-title">
              <h3>${index + 1}. ${escapeHtml(item.name || "Unnamed species")}</h3>
              <span class="tag ${criticalWarnings.length ? "is-danger" : "is-info"} is-light">${scoreLabel}</span>
            </div>
            <p>${escapeHtml(readableDetails(item))}</p>
            <div class="score-line">
              <span>${scoreLabel}</span>
              <div class="score-bar" aria-hidden="true"><div class="score-fill" style="width:${shownScore}%"></div></div>
            </div>
            <div class="meta-grid" style="margin-top:.7rem">
              ${metaItem("Rule coverage", `${formatNumber(confidence)}%`)}
              ${metaItem("Safety", safetyLabel)}
              ${metaItem("Priority", item.priority_column || "--")}
              ${metaItem("Compared", Array.isArray(item.used_sensors) ? item.used_sensors.join(", ") : "--")}
            </div>
            ${warningHtml}
            ${detailHtml}
          </div>
        </article>
      `;
    }).join("");
  }

  function normalizeRecommendationPayload(response) {
    let result = response?.result || response || {};
    if (result?.result && Array.isArray(result.result.suggestions)) result = result.result;
    return result || {};
  }

  // The camera transform affects only the visual preview, never the frames
  // sent to the existing weight scanner or its underlying prediction logic.
  function bindCameraViewControls() {
    $("#rotateCameraButton")?.addEventListener("click", () => {
      state.cameraRotation = (state.cameraRotation + 90) % 360;
      saveCameraView();
      applyCameraView();
    });
    $("#mirrorCameraButton")?.addEventListener("click", () => {
      state.cameraMirrored = !state.cameraMirrored;
      saveCameraView();
      applyCameraView();
    });
    $("#resetCameraButton")?.addEventListener("click", () => {
      state.cameraRotation = 0;
      state.cameraMirrored = false;
      saveCameraView();
      applyCameraView();
    });
    window.addEventListener("resize", applyCameraView);
    applyCameraView();
  }

  function saveCameraView() {
    localStorage.setItem("ac_camera_rotation", String(state.cameraRotation));
    localStorage.setItem("ac_camera_mirrored", String(state.cameraMirrored));
  }

  function applyCameraView() {
    const cameraFeed = $("#cameraFeed");
    const frame = $("#cameraFrame");
    if (!cameraFeed || !frame) return;
    const bounds = frame.getBoundingClientRect();
    // At 90/270 degrees, fit the rotated preview within the camera frame.
    const fit = state.cameraRotation % 180 && bounds.width && bounds.height
      ? Math.min(bounds.width / bounds.height, bounds.height / bounds.width)
      : 1;
    cameraFeed.style.transform = `scale(${fit}) rotate(${state.cameraRotation}deg) scaleX(${state.cameraMirrored ? -1 : 1})`;
    cameraFeed.dataset.rotation = String(state.cameraRotation);
    cameraFeed.dataset.mirrored = String(state.cameraMirrored);
    $("#mirrorCameraButton")?.setAttribute("aria-pressed", String(state.cameraMirrored));
    $("#mirrorCameraButton")?.classList.toggle("is-selected", state.cameraMirrored);
    setText("#cameraOrientation", `${state.cameraRotation}° · ${state.cameraMirrored ? "Mirrored" : "Normal"}`);
  }

  async function getJson(url) {
    try {
      const headers = { "Accept": "application/json" };
      if (state.adminToken) headers["X-AquaCulture-Admin-Token"] = state.adminToken;
      const response = await fetch(url, { headers, cache: "no-store" });
      const data = await response.json().catch(() => ({}));
      handleAuthFailure(response.status, data);
      return { ok: response.ok && data.ok !== false, status: response.status, ...data };
    } catch (error) {
      return { ok: false, message: error.message || "Request failed." };
    }
  }

  async function postJson(url, data, options = {}) {
    const timeoutMs = Number(options.timeoutMs) || 0;
    const controller = timeoutMs > 0 ? new AbortController() : null;
    const timeoutId = controller ? window.setTimeout(() => controller.abort(), timeoutMs) : null;
    try {
      const headers = { "Accept": "application/json", "Content-Type": "application/json" };
      if (state.adminToken) headers["X-AquaCulture-Admin-Token"] = state.adminToken;
      const response = await fetch(url, {
        method: "POST",
        headers,
        body: JSON.stringify(data || {}),
        ...(controller ? { signal: controller.signal } : {}),
      });
      const payload = await response.json().catch(() => ({}));
      handleAuthFailure(response.status, payload);
      return { ok: response.ok && payload.ok !== false, status: response.status, ...payload };
    } catch (error) {
      return { ok: false, timed_out: error?.name === "AbortError",
        message: error?.name === "AbortError"
          ? "The scan response exceeded the selected time plus connection allowance. Check the camera, model, or network connection."
          : error.message || "Request failed." };
    } finally {
      if (timeoutId !== null) window.clearTimeout(timeoutId);
    }
  }


  function setLoginMessage(message = "", isError = false) {
    setText("#loginMessage", message);
    $("#loginMessage")?.classList.toggle("is-error", isError);
    $("#adminPassword")?.setAttribute("aria-invalid", String(isError));
  }

  function openDialog(id) {
    const dialog = document.getElementById(id);
    if (!dialog) return;
    if (id === "loginModal") {
      syncAuthUi();
      if (!dialog.open) {
        $("#loginForm")?.reset();
        setLoginMessage();
      }
    }
    if (dialog.showModal && !dialog.open) dialog.showModal();
  }

  function closeDialog(id) {
    const dialog = id ? document.getElementById(id) : null;
    if (dialog?.close) dialog.close();
  }

  async function logoutAdmin() {
    if (state.loggingOut) return;
    state.loggingOut = true;
    state.authRevision += 1;
    state.loggedIn = false;
    syncAuthUi();
    const result = await postJson("/api/admin/logout", { admin_token: state.adminToken });
    if (!result.ok && result.status !== 401) {
      state.loggingOut = false;
      await checkAdminSession();
      showToast(result.message || "Could not end the admin session. Please try again.", "error");
      return;
    }
    clearAdminSession(true);
    state.loggingOut = false;
    syncAuthUi();
    closeDialog("loginModal");
    showToast("Logged out. Settings, records, and scan controls are locked.", "info");
  }

  function clearAdminSession(notify) {
    state.authRevision += 1;
    window.clearTimeout(state.sessionExpiryTimer);
    state.adminToken = "";
    state.adminSession = null;
    state.loggedIn = false;
    localStorage.removeItem("aquaculture_admin_token");
    localStorage.removeItem("aquaculture_admin_session");
    localStorage.removeItem("aquaculture_admin");
    if (notify) showToast("Admin session cleared.", "info");
  }

  function requireAdminForScan() {
    if (state.loggedIn) return true;
    showToast("Login required before scanning.", "warning");
    syncAuthUi();
    openDialog("loginModal");
    return false;
  }

  function handleAuthFailure(status, data) {
    if (status === 401 && data && data.session_required) {
      clearAdminSession(false);
      syncAuthUi();
      showToast(data.message || "Admin login is required.", "warning");
    }
  }

  function showToast(message, type = "info") {
    if (window.AquaToast && typeof window.AquaToast.show === "function") {
      window.AquaToast.show(message, type);
      return;
    }
    console.log(`[${type}] ${message}`);
  }

  function setText(selector, text) {
    const element = $(selector);
    if (element) element.textContent = text;
  }

  function setHtml(selector, html) {
    const element = $(selector);
    if (element) element.innerHTML = html;
  }

  function setBusy(button, busy, label) {
    if (!button) return;
    button.disabled = busy;
    button.classList.toggle("is-loading", Boolean(busy));
    button.textContent = label;
  }


  function numberOrNull(selector) {
    const raw = $(selector)?.value;
    if (raw === null || raw === undefined || String(raw).trim() === "") return null;
    const number = Number(raw);
    return Number.isFinite(number) ? number : null;
  }

  function numberInput(selector, fallback, min, max) {
    const value = Number($(selector)?.value || fallback);
    if (!Number.isFinite(value)) return fallback;
    return Math.max(min, Math.min(max, value));
  }

  function formatValue(value, suffix) {
    if (value === null || value === undefined || value === "") return "--";
    return `${formatNumber(value)}${suffix}`;
  }

  function formatNumber(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return String(value ?? "--");
    if (Math.abs(number) >= 100) return number.toFixed(0);
    if (Math.abs(number) >= 10) return number.toFixed(1);
    return number.toFixed(2).replace(/\.00$/, "");
  }

  function percentOrNumber(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "--";
    return number <= 1 ? `${formatNumber(number * 100)}%` : `${formatNumber(number)}%`;
  }

  function cm(value) {
    return value === null || value === undefined ? "--" : `${formatNumber(value)} cm`;
  }

  function grams(value) {
    return value === null || value === undefined ? "--" : `${formatNumber(value)} g`;
  }

  function metaItem(label, value) {
    return `<div class="meta-item"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value ?? "--")}</strong></div>`;
  }

  function pick(object, keys) {
    for (const key of keys) {
      if (Object.prototype.hasOwnProperty.call(object, key)) return object[key];
    }
    return undefined;
  }

  function clampNumber(value, min, max) {
    const number = Number(value);
    if (!Number.isFinite(number)) return min;
    return Math.max(min, Math.min(max, number));
  }

  function errorBlock(message) {
    return `<div class="empty-state is-bad">${escapeHtml(message)}</div>`;
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function escapeAttr(value) {
    return escapeHtml(value).replaceAll("`", "&#096;");
  }
})();
