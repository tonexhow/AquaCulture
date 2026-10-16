(() => {
  "use strict";

  const $ = (selector, parent = document) => parent.querySelector(selector);
  const $$ = (selector, parent = document) => Array.from(parent.querySelectorAll(selector));

  const SENSOR_META = [
    { key: "Temperature", label: "Temperature", unit: "°C", placeholder: "24–32", step: "0.01" },
    { key: "PH", label: "pH", unit: "pH", placeholder: "6.5–8.5", step: "0.01" },
    { key: "Turbidity", label: "Turbidity", unit: "NTU", placeholder: "0–80", step: "0.01" },
    { key: "Ammonia", label: "Ammonia", unit: "ppm", placeholder: "0–0.05", step: "0.001" },
    { key: "Oxygen", label: "Oxygen", unit: "mg/L", placeholder: "5–10", step: "0.01" },
  ];

  const DEFAULT_NOTIFICATION = {
    Temperature: { min: { value: 24, msg: "Water temperature is below the safe range. Average: !{average_reading} °C at !{time}." }, max: { value: 32, msg: "Water temperature is above the safe range. Average: !{average_reading} °C at !{time}." }, status: 1 },
    PH: { min: { value: 6.5, msg: "Water pH is below the safe range. Average: !{average_reading} at !{time}." }, max: { value: 8.5, msg: "Water pH is above the safe range. Average: !{average_reading} at !{time}." }, status: 1 },
    Turbidity: { min: { value: 0, msg: "Water turbidity is below the configured range. Average: !{average_reading} NTU at !{time}." }, max: { value: 80, msg: "Water turbidity is above the configured range. Average: !{average_reading} NTU at !{time}." }, status: 1 },
    Ammonia: { min: { value: 0, msg: "Ammonia is below the configured range. Average: !{average_reading} ppm at !{time}." }, max: { value: 0.05, msg: "Ammonia is above the safe range. Average: !{average_reading} ppm at !{time}." }, status: 1 },
    Oxygen: { min: { value: 5, msg: "Dissolved oxygen is below the safe range. Average: !{average_reading} mg/L at !{time}." }, max: { value: 10, msg: "Dissolved oxygen is above the configured range. Average: !{average_reading} mg/L at !{time}." }, status: 1 },
    Summary: 86400,
    Gap: 60,
    Hold: 5,
    Email: 1,
    SMS: 0,
  };

  const state = {
    token: localStorage.getItem("aquaculture_admin_token") || "",
    session: null,
    activeTab: "notification",
    notification: structuredClone(DEFAULT_NOTIFICATION),
    subscribers: [],
    species: [],
    referenceSpecies: [],
    selectedSpecies: null,
    selectedImageUrl: "",
    libraryFolder: "species",
    libraryFolders: [],
    libraryFiles: [],
    libraryRequest: 0,
    sessionExpiryTimer: null,
    tables: [],
    activeTable: "",
    tableOffset: 0,
    tableTotal: 0,
    editingNotificationField: null,
  };

  document.addEventListener("DOMContentLoaded", init);

  async function init() {
    applyTheme();
    bindShell();
    bindNotifications();
    bindSpecies();
    bindDatabase();
    bindLibrary();
    bindAccess();
    const allowed = await requireSession();
    if (!allowed) return;
    window.setInterval(requireSession, 60_000);
    window.addEventListener("pageshow", (event) => { if (event.persisted) requireSession(); });
    window.addEventListener("storage", (event) => {
      if (event.key !== "aquaculture_admin_token") return;
      state.token = event.newValue || "";
      if (!state.token) leaveSettings();
      else requireSession();
    });
    await loadAll();
    const recovery = await apiGet("/api/admin/recovery-email");
    if (recovery.ok) setValue("#adminRecoveryEmail", recovery.email || "");
  }

  function applyTheme() {
    window.AquaUI?.applyTheme();
  }

  function bindShell() {
    const header = $(".settings-header");
    const hamburger = $("#hamburgerButton");
    let menuScrollY = 0;

    const lockMenuScroll = () => {
      menuScrollY = window.scrollY || document.documentElement.scrollTop || 0;
      document.documentElement.classList.add("menu-locked");
      document.body.classList.add("menu-locked");
      document.body.style.position = "fixed";
      document.body.style.top = `-${menuScrollY}px`;
      document.body.style.left = "0";
      document.body.style.right = "0";
      document.body.style.width = "100%";
    };

    const unlockMenuScroll = () => {
      const restoreY = Math.abs(parseInt(document.body.style.top || "0", 10)) || menuScrollY || 0;
      document.documentElement.classList.remove("menu-locked");
      document.body.classList.remove("menu-locked");
      document.body.style.position = "";
      document.body.style.top = "";
      document.body.style.left = "";
      document.body.style.right = "";
      document.body.style.width = "";
      window.scrollTo(0, restoreY);
    };

    const setMenuOpen = (open) => {
      const alreadyOpen = Boolean(header?.classList.contains("menu-open"));
      if (open === alreadyOpen) return;
      if (open) lockMenuScroll();
      header?.classList.toggle("menu-open", open);
      hamburger?.classList.toggle("is-active", open);
      hamburger?.setAttribute("aria-expanded", String(open));
      if (!open) unlockMenuScroll();
    };

    const closeMenu = () => setMenuOpen(false);

    hamburger?.addEventListener("click", () => {
      setMenuOpen(!header?.classList.contains("menu-open"));
    });

    $$('[data-tab]').forEach((button) => {
      button.addEventListener("click", () => {
        showTab(button.dataset.tab || "notification");
        closeMenu();
      });
    });
    $$(".settings-nav .nav-link").forEach((link) => link.addEventListener("click", closeMenu));
    window.addEventListener("resize", () => {
      if (window.innerWidth >= 760) closeMenu();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeMenu();
    });
    $("#refreshButton")?.addEventListener("click", loadAll);
    $("#logoutButton")?.addEventListener("click", logout);
    $$('[data-close-modal]').forEach((button) => button.addEventListener("click", () => closeModal(button.dataset.closeModal)));
  }

  function showTab(tab) {
    state.activeTab = tab;
    $$('[data-tab]').forEach((button) => button.classList.toggle("is-active", button.dataset.tab === tab));
    $$('[data-panel]').forEach((panel) => {
      const active = panel.dataset.panel === tab;
      panel.hidden = !active;
      panel.classList.toggle("is-active", active);
    });
  }

  async function requireSession() {
    const result = await apiGet("/api/admin/session");
    if (!result.ok && result.status !== 401) return false;
    if (!result.ok || !result.session_active) {
      toast("Login required", "Open the main page and log in first.", "warning");
      leaveSettings();
      return false;
    }
    state.session = result;
    const main = $(".settings-main");
    if (main) main.hidden = false;
    $("#sessionLabel").textContent = `Session #${result.frontend_session_id || "--"}`;
    window.clearTimeout(state.sessionExpiryTimer);
    const remaining = Number(result.expires_in_seconds);
    if (Number.isFinite(remaining)) state.sessionExpiryTimer = window.setTimeout(leaveSettings, Math.max(0, remaining * 1000));
    return true;
  }

  function leaveSettings() {
    window.clearTimeout(state.sessionExpiryTimer);
    state.session = null;
    const main = $(".settings-main");
    if (main) main.hidden = true;
    localStorage.removeItem("aquaculture_admin_token");
    localStorage.removeItem("aquaculture_admin_session");
    window.location.replace("/?login=settings-required");
  }

  async function loadAll() {
    await Promise.all([loadNotification(), loadSubscribers(), loadSpecies(), loadReferenceSpecies(), loadTables(), loadLibrary()]);
  }

  function bindNotifications() {
    $("#saveNotificationButton")?.addEventListener("click", saveNotification);
    $("#reloadSubscribersButton")?.addEventListener("click", loadSubscribers);
    $("#emailToggle")?.addEventListener("click", () => toggleDelivery("Email"));
    $("#smsToggle")?.addEventListener("click", () => toggleDelivery("SMS"));
    $$('[data-open-subscriber]').forEach((button) => button.addEventListener("click", () => openSubscriberModal(button.dataset.openSubscriber)));
    $$('[data-test-channel]').forEach((button) => button.addEventListener("click", () => testNotification(button.dataset.testChannel, button)));
    $("#subscriberForm")?.addEventListener("submit", saveSubscriber);
    $("#notificationFieldForm")?.addEventListener("submit", saveNotificationFieldModal);
  }

  async function loadNotification() {
    const result = await apiGet("/api/admin/notification");
    state.notification = normalizeNotification(result.notification || DEFAULT_NOTIFICATION);
    renderNotification();
    renderAlertStatus(result.status || result.alert_status || {});
  }

  function normalizeNotification(raw) {
    const clean = structuredClone(DEFAULT_NOTIFICATION);
    for (const sensor of SENSOR_META) {
      const incoming = raw?.[sensor.key] || {};
      clean[sensor.key].status = Number(incoming.status ?? clean[sensor.key].status) ? 1 : 0;
      for (const bound of ["min", "max"]) {
        clean[sensor.key][bound].value = numberOr(incoming?.[bound]?.value, clean[sensor.key][bound].value);
        clean[sensor.key][bound].msg = String(incoming?.[bound]?.msg ?? clean[sensor.key][bound].msg ?? "");
      }
    }
    clean.Summary = intOr(raw?.Summary, clean.Summary);
    clean.Gap = intOr(raw?.Gap, clean.Gap);
    clean.Hold = intOr(raw?.Hold, clean.Hold);
    clean.Email = Number(raw?.Email ?? clean.Email) ? 1 : 0;
    clean.SMS = Number(raw?.SMS ?? clean.SMS) ? 1 : 0;
    return clean;
  }

  function renderNotification() {
    const target = $("#notificationSensors");
    if (!target) return;
    target.innerHTML = SENSOR_META.map((sensor) => renderSensorCard(sensor)).join("");
    target.querySelectorAll("[data-sensor-toggle]").forEach((button) => {
      button.addEventListener("click", () => {
        const key = button.dataset.sensorToggle;
        state.notification[key].status = state.notification[key].status ? 0 : 1;
        renderNotification();
      });
    });
    target.querySelectorAll("[data-alert-edit]").forEach((button) => {
      button.addEventListener("click", () => {
        const [sensor, bound] = button.dataset.alertEdit.split(":");
        openNotificationFieldModal(sensor, bound);
      });
    });
    setValue("#notifyHold", state.notification.Hold);
    setValue("#notifyGap", state.notification.Gap);
    setValue("#notifySummary", state.notification.Summary);
    renderSummaryDuration();
    ["#notifyHold", "#notifyGap", "#notifySummary"].forEach((selector) => {
      const input = $(selector);
      if (input && !input.dataset.boundOnce) {
        input.dataset.boundOnce = "1";
        input.addEventListener("input", syncTimingRaw);
      }
    });
    renderDeliveryToggles();
  }

  function renderSensorCard(sensor) {
    const cfg = state.notification[sensor.key];
    const on = Boolean(cfg.status);
    return `
      <article class="box notification-card">
        <div class="notification-card-head">
          <div>
            <h3>${escapeHtml(sensor.label)}</h3>
            <p>${escapeHtml(sensor.placeholder)} ${escapeHtml(sensor.unit)}</p>
          </div>
          <button class="toggle button is-rounded ${on ? "is-on" : ""}" type="button" data-sensor-toggle="${escapeAttr(sensor.key)}">${on ? "ON" : "OFF"}</button>
        </div>
        <div class="bound-grid">
          ${boundEditor(sensor, "min", cfg.min)}
          ${boundEditor(sensor, "max", cfg.max)}
        </div>
      </article>`;
  }

  function boundEditor(sensor, bound, data) {
    const label = bound === "min" ? "Minimum" : "Maximum";
    return `
      <button class="alert-edit-field" type="button" data-alert-edit="${escapeAttr(sensor.key)}:${escapeAttr(bound)}">
        <span>${label} ${escapeHtml(sensor.unit)}</span>
        <strong>${escapeHtml(data.value)} ${escapeHtml(sensor.unit)}</strong>
        <small>${escapeHtml(shortText(data.msg, 92))}</small>
      </button>`;
  }

  function openNotificationFieldModal(sensorKey, bound) {
    const cfg = state.notification?.[sensorKey]?.[bound];
    if (!cfg) return;
    state.editingNotificationField = { sensorKey, bound };
    const sensor = SENSOR_META.find((item) => item.key === sensorKey) || { label: sensorKey, unit: "" };
    setText("#notificationFieldTitle", `${sensor.label} ${bound === "min" ? "minimum" : "maximum"} alert`);
    setValue("#notificationFieldSensor", sensorKey);
    setValue("#notificationFieldBound", bound);
    setValue("#notificationFieldValue", cfg.value);
    setValue("#notificationFieldMessage", cfg.msg);
    openModal("notificationFieldModal");
  }

  function saveNotificationFieldModal(event) {
    event.preventDefault();
    const sensorKey = $("#notificationFieldSensor")?.value || state.editingNotificationField?.sensorKey;
    const bound = $("#notificationFieldBound")?.value || state.editingNotificationField?.bound;
    if (!state.notification?.[sensorKey]?.[bound]) return closeModal("notificationFieldModal");
    state.notification[sensorKey][bound].value = numberOr($("#notificationFieldValue")?.value, state.notification[sensorKey][bound].value);
    state.notification[sensorKey][bound].msg = String($("#notificationFieldMessage")?.value || "").trim();
    closeModal("notificationFieldModal");
    renderNotification();
    toast("Alert field updated", "Press Save notifications to store the change.", "success");
  }

  function syncTimingFromInputs() {
    state.notification.Hold = intOr($("#notifyHold")?.value, 5);
    state.notification.Gap = intOr($("#notifyGap")?.value, 60);
    state.notification.Summary = intOr($("#notifySummary")?.value, 86400);
    renderNotification();
  }

  function toggleDelivery(key) {
    state.notification[key] = state.notification[key] ? 0 : 1;
    renderDeliveryToggles();
  }

  function renderDeliveryToggles() {
    const email = $("#emailToggle");
    const sms = $("#smsToggle");
    if (email) {
      email.textContent = state.notification.Email ? "Email ON" : "Email OFF";
      email.classList.toggle("is-on", Boolean(state.notification.Email));
    }
    if (sms) {
      sms.textContent = state.notification.SMS ? "SMS ON" : "SMS OFF";
      sms.classList.toggle("is-on", Boolean(state.notification.SMS));
    }
  }

  function renderAlertStatus(status) {
    const chip = $("#alertStatusChip");
    if (chip) {
      chip.textContent = status?.last_status?.message || status?.message || "Ready";
      chip.className = `status-chip ${status?.ok === false ? "warn" : "good"}`;
    }
    const env = status?.environment || {};
    const emailReady = env.email?.email_ready ? "Email ready" : "Email .env missing";
    const smsReady = env.sms?.sms_ready ? "SMS ready" : "SMS .env missing";
    setText("#envStatus", `${emailReady} · ${smsReady}`);
  }

  async function saveNotification() {
    for (const selector of ["#notifyHold", "#notifyGap", "#notifySummary"]) {
      const input = $(selector);
      if (selector === "#notifySummary") {
        const value = Number(input.value);
        input.setCustomValidity(input.value.trim() && Number.isInteger(value) && (value === -1 || value >= 1 && value <= 31536000) ? "" : "Enter whole seconds from 1 to 31536000, or -1 to disable.");
      }
      if (!input.reportValidity()) return;
    }
    syncTimingRaw();
    const button = $("#saveNotificationButton");
    setBusy(button, true, "Saving");
    const result = await apiJson("/api/admin/notification", "PATCH", state.notification);
    setBusy(button, false, "Save notifications");
    if (result.ok) {
      state.notification = normalizeNotification(result.notification || state.notification);
      renderNotification();
      toast("Notifications saved", result.message || "Alert settings updated.", "success");
    } else {
      toast("Save failed", result.message || "Notification settings were not saved.", "error");
    }
  }

  function syncTimingRaw() {
    state.notification.Hold = intOr($("#notifyHold")?.value, state.notification.Hold);
    state.notification.Gap = intOr($("#notifyGap")?.value, state.notification.Gap);
    state.notification.Summary = intOr($("#notifySummary")?.value, state.notification.Summary);
    $("#notifySummary")?.setCustomValidity("");
    renderSummaryDuration();
  }

  function renderSummaryDuration() {
    const value = Number($("#notifySummary")?.value);
    const text = value === -1 ? "Periodic summaries disabled; safety alerts remain active." :
      Number.isInteger(value) && value > 0 ? `Equivalent to: ${Number((value / 3600).toFixed(3))} hours (${Number((value / 86400).toFixed(3))} days)` : "Enter a positive whole number of seconds, or -1 to disable.";
    setText("#summaryDuration", text);
  }

  async function loadSubscribers() {
    const result = await apiGet("/api/subscribers");
    state.subscribers = Array.isArray(result.subscribers) ? result.subscribers : [];
    renderSubscribers();
  }

  function renderSubscribers() {
    const target = $("#subscriberList");
    if (!target) return;
    if (!state.subscribers.length) {
      target.innerHTML = `<div class="empty">No subscribers saved yet.</div>`;
      return;
    }
    target.innerHTML = state.subscribers.map((row) => `
      <div class="subscriber-item">
        <div><strong>${escapeHtml(row.contact)}</strong><p>${escapeHtml(row.type)} · ID ${escapeHtml(row.id)}</p></div>
        <div class="button-row">
          <button class="button is-light is-rounded is-small" type="button" data-edit-subscriber="${escapeAttr(row.id)}">Edit</button>
          <button class="button is-danger is-light is-rounded is-small" type="button" data-delete-subscriber="${escapeAttr(row.id)}">Delete</button>
        </div>
      </div>`).join("");
    target.querySelectorAll("[data-edit-subscriber]").forEach((button) => button.addEventListener("click", () => {
      const row = state.subscribers.find((item) => String(item.id) === String(button.dataset.editSubscriber));
      if (row) openSubscriberModal(row.type, row);
    }));
    target.querySelectorAll("[data-delete-subscriber]").forEach((button) => button.addEventListener("click", () => deleteSubscriber(button.dataset.deleteSubscriber)));
  }

  function openSubscriberModal(type, row = null) {
    setText("#subscriberModalTitle", row ? "Edit contact" : `Add ${type} contact`);
    setText("#subscriberContactLabel", type === "SMS" ? "Phone number" : "Email address");
    setValue("#subscriberId", row?.id || "");
    setValue("#subscriberType", String(type || row?.type || "EMAIL").toUpperCase());
    setValue("#subscriberContact", row?.contact || "");
    openModal("subscriberModal");
  }

  async function saveSubscriber(event) {
    event.preventDefault();
    const id = $("#subscriberId")?.value || "";
    const type = $("#subscriberType")?.value || "EMAIL";
    const contact = $("#subscriberContact")?.value || "";
    const result = id ? await apiJson(`/api/subscribers/${encodeURIComponent(id)}`, "PATCH", { type, contact }) : await apiJson("/api/subscribers", "POST", { type, contact });
    if (result.ok) {
      closeModal("subscriberModal");
      await loadSubscribers();
      toast("Subscriber saved", result.message || "Contact saved.", "success");
    } else {
      toast("Save failed", result.message || "Contact was not saved.", "error");
    }
  }

  async function deleteSubscriber(id) {
    if (!confirm("Delete this subscriber?")) return;
    const result = await apiJson(`/api/subscribers/${encodeURIComponent(id)}`, "DELETE");
    if (result.ok) {
      await loadSubscribers();
      toast("Subscriber removed", result.message || "Contact removed.", "success");
    } else {
      toast("Delete failed", result.message || "Contact was not removed.", "error");
    }
  }

  async function testNotification(channel, button) {
    setBusy(button, true, "Testing");
    const result = await apiJson("/api/admin/notification/test", "POST", { channel });
    setBusy(button, false, `Test ${channel === "SMS" ? "SMS" : "Email"}`);
    toast(result.ok ? "Test processed" : "Test warning", summarizeResults(result), result.ok ? "success" : "warning");
  }

  function summarizeResults(result) {
    if (Array.isArray(result.results)) return result.results.map((item) => `${item.channel || "Channel"}: ${item.message || item.sent || 0}`).join(" · ");
    return result.message || "Done.";
  }

  function bindSpecies() {
    $("#newSpeciesButton")?.addEventListener("click", () => {
      clearSpeciesForm();
      scrollToSpeciesEditor();
    });
    $("#clearSpeciesFormButton")?.addEventListener("click", clearSpeciesForm);
    $("#deleteSpeciesButton")?.addEventListener("click", deleteSpecies);
    $("#speciesForm")?.addEventListener("submit", saveSpecies);
    $("#speciesSearch")?.addEventListener("input", () => { renderSpeciesTable(); renderReferenceSpecies(); });
    $("#speciesImage")?.addEventListener("change", previewSelectedSpeciesImage);
    $("#scanDuplicateSpeciesButton")?.addEventListener("click", scanDuplicateSpecies);
    $("#exportSpeciesButton")?.addEventListener("click", () => downloadTableCsv("species"));
  }

  async function loadSpecies() {
    const result = await apiGet("/api/species?include_image=0");
    state.species = Array.isArray(result.species) ? result.species : [];
    renderSpeciesTable();
  }

  async function loadReferenceSpecies() {
    const result = await apiGet("/api/species/reference");
    state.referenceSpecies = Array.isArray(result.species) ? result.species : [];
    setText("#referenceSpeciesCount", `${state.referenceSpecies.length} species`);
    renderReferenceSpecies();
  }

  function renderReferenceSpecies() {
    const target = $("#speciesReferenceTable");
    if (!target) return;
    const query = String($("#speciesSearch")?.value || "").trim().toLowerCase();
    const rows = state.referenceSpecies.filter((row) => [row.common_name, row.scientific_name, row.habitat].some((value) => String(value || "").toLowerCase().includes(query)));
    if (!rows.length) {
      target.innerHTML = `<div class="empty">${query ? "No research species match your search." : "The research catalog is unavailable. Refresh to try again."}</div>`;
      return;
    }
    target.innerHTML = `<table class="table reference-species-table"><thead><tr><th>Common name</th><th>Scientific name</th><th>Habitat / scope</th><th>Source</th><th>Rules</th></tr></thead><tbody>${rows.map((row) => `
      <tr><td><strong>${escapeHtml(row.common_name)}</strong></td><td><em>${escapeHtml(row.scientific_name)}</em></td><td>${escapeHtml(row.habitat)}<small class="reference-scope">${escapeHtml(row.catalog_scope || "")}</small></td><td>${sourceLink(row)}</td><td><button type="button" class="button is-info is-light is-small" data-configure-reference="${escapeAttr(row.id)}">Configure rules</button></td></tr>`).join("")}</tbody></table>`;
    target.querySelectorAll("[data-configure-reference]").forEach((button) => button.addEventListener("click", () => configureReferenceSpecies(button.dataset.configureReference)));
  }

  function sourceLink(row) {
    const url = externalSourceUrl(row.source_url);
    return url ? `<a href="${escapeAttr(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(row.source_title || "Research source")} ↗</a>` : "Source pending";
  }

  function externalSourceUrl(value) {
    try { const url = new URL(String(value || "")); return url.protocol === "https:" || url.protocol === "http:" ? url.href : ""; }
    catch (_) { return ""; }
  }

  function configureReferenceSpecies(id) {
    const row = state.referenceSpecies.find((item) => String(item.id) === String(id));
    if (!row) return;
    const names = [row.common_name, row.name, row.scientific_name, `${row.common_name} (${row.scientific_name})`, ...(Array.isArray(row.aliases) ? row.aliases : [])];
    const existing = state.species.find((item) => names.some((name) => String(name || "").trim().toLowerCase() === String(item.name || "").trim().toLowerCase()));
    if (existing) { editSpecies(existing.id); return; }
    clearSpeciesForm();
    setText("#speciesFormTitle", `Configure ${row.common_name}`);
    setValue("#speciesName", `${row.common_name} (${row.scientific_name})`);
    toast("Rules needed", "Enter validated ranges and scoring weights for each water parameter before saving.", "info");
    scrollToSpeciesEditor();
  }

  function renderSpeciesTable() {
    const target = $("#speciesTable");
    if (!target) return;
    const query = String($("#speciesSearch")?.value || "").toLowerCase();
    const rows = state.species.filter((row) => String(row.name || "").toLowerCase().includes(query));
    if (!rows.length) {
      target.innerHTML = `<div class="empty">No species found.</div>`;
      return;
    }
    const columns = [
      ["image", "Image"],
      ["name", "Name"],
      ["temperature", "Temperature"],
      ["ph", "pH"],
      ["oxygen", "Oxygen"],
      ["turbidity", "Turbidity"],
      ["ammonia", "Ammonia"],
      ["environment", "Environment"],
      ["regulation", "Regulation"],
      ["ecology", "Ecology"],
      ["safety", "Safety"],
    ];
    target.innerHTML = `
      <table class="table is-hoverable species-full-table">
        <thead>
          <tr><th>Action</th>${columns.map(([, label]) => `<th>${escapeHtml(label)}</th>`).join("")}</tr>
        </thead>
        <tbody>
          ${rows.map((row) => `
            <tr class="${row.has_image ? "has-image" : "missing-image"} ${String(state.selectedSpecies?.id || "") === String(row.id) ? "is-selected-row" : ""}">
              <td class="species-action-cell"><button class="button is-primary is-light is-rounded is-small" type="button" data-select-species="${escapeAttr(row.id)}">Edit</button></td>
              ${columns.map(([key]) => `<td data-label="${escapeAttr(key)}">${renderSpeciesCell(row, key)}</td>`).join("")}
            </tr>`).join("")}
        </tbody>
      </table>`;
    target.querySelectorAll("[data-select-species]").forEach((button) => button.addEventListener("click", () => editSpecies(button.dataset.selectSpecies)));
  }

  function renderSpeciesCell(row, key) {
    if (key === "image") return renderSpeciesImageCell(row);
    if (key === "name") return `<strong>${escapeHtml(row[key] || "")}</strong>`;
    return `<span class="cell-text">${escapeHtml(formatCell(row[key]))}</span>`;
  }

  function renderSpeciesImageCell(row) {
    if (row?.has_image && row?.id) {
      const src = `/api/species/${encodeURIComponent(row.id)}/image?cache=${encodeURIComponent(row.updated_at || "")}`;
      return `
        <button class="species-thumb-button" type="button" data-select-species="${escapeAttr(row.id)}" aria-label="Preview ${escapeAttr(row.name || "species")} image">
          <img class="species-thumb previewable-image" data-image-preview="1" loading="lazy" src="${escapeAttr(src)}" alt="${escapeAttr(row.name || "Species")} image" onerror="this.closest('td').innerHTML='<span class=\'image-badge missing\'>Missing</span>'">
          <span class="image-badge good">Has image</span>
        </button>`;
    }
    return `<span class="image-badge missing">No image</span>`;
  }

  function formatCell(value) {
    if (value === null || value === undefined || value === "") return "--";
    if (Array.isArray(value)) return JSON.stringify(value);
    if (typeof value === "object") return JSON.stringify(value);
    return String(value);
  }


  async function scanDuplicateSpecies() {
    const button = $("#scanDuplicateSpeciesButton");
    const box = $("#duplicateSpeciesBox");
    setBusy(button, true, "Scanning");
    const result = await apiJson("/api/admin/species/duplicates", "POST", {});
    setBusy(button, false, "Scan duplicates");
    if (!box) return;
    box.hidden = false;
    const duplicates = Array.isArray(result.duplicates) ? result.duplicates : [];
    if (result.ok && duplicates.length === 0) {
      box.innerHTML = `<strong>No duplicate species names found.</strong><p>All saved species names are unique.</p>`;
      toast("Duplicate scan complete", "No duplicate names found.", "success");
      return;
    }
    if (!result.ok) {
      box.innerHTML = `<strong>Duplicate scan failed.</strong><p>${escapeHtml(result.message || "Please try again.")}</p>`;
      toast("Duplicate scan failed", result.message || "Please try again.", "error");
      return;
    }
    box.innerHTML = `<strong>${duplicates.length} duplicate name group(s) found.</strong>${duplicates.map((group) => `
      <div class="duplicate-group">
        <p><b>${escapeHtml(group.name)}</b> · ${escapeHtml(group.count)} records</p>
        <small>${(group.items || []).map((item) => `ID ${escapeHtml(item.id)}`).join(", ")}</small>
      </div>`).join("")}`;
    toast("Duplicate scan complete", `${duplicates.length} duplicate group(s) found.`, "warning");
  }

  async function editSpecies(id) {
    const result = await apiGet(`/api/species/${encodeURIComponent(id)}`);
    const row = result.species || result.row || state.species.find((item) => String(item.id) === String(id));
    if (!row) return;
    state.selectedSpecies = row;
    if ($("#speciesImage")) $("#speciesImage").value = "";
    setText("#speciesFormTitle", `Editing ${row.name || "species"}`);
    setValue("#speciesId", row.id || "");
    setValue("#speciesName", row.name || "");
    setValue("#speciesTemperature", row.temperature || "");
    setValue("#speciesPh", row.ph || "");
    setValue("#speciesOxygen", row.oxygen || "");
    setValue("#speciesTurbidity", row.turbidity || "");
    setValue("#speciesAmmonia", row.ammonia || "");
    setValue("#speciesEnvironment", row.environment || "[]");
    setValue("#speciesRegulation", row.regulation || "[]");
    setValue("#speciesEcology", row.ecology || "[]");
    setValue("#speciesSafety", row.safety || "[]");
    renderSpeciesCurrentImage(row);
    renderSpeciesTable();
    scrollToSpeciesEditor();
  }

  function scrollToSpeciesEditor() {
    const editor = $("#speciesEditorCard") || $(".species-form-card");
    if (!editor) return;
    window.requestAnimationFrame(() => {
      editor.scrollIntoView({ behavior: "smooth", block: "start", inline: "nearest" });
      const nameInput = $("#speciesName");
      if (nameInput && window.matchMedia("(min-width: 760px)").matches) {
        setTimeout(() => nameInput.focus({ preventScroll: true }), 250);
      }
    });
  }

  function renderSpeciesCurrentImage(row) {
    releaseImagePreview();
    const box = $("#speciesCurrentImage");
    if (!box) return;
    if (row?.has_image && row?.id) {
      const src = `/api/species/${encodeURIComponent(row.id)}/image?cache=${encodeURIComponent(row.updated_at || "")}`;
      box.innerHTML = `
        <div class="current-image-card">
          <img class="previewable-image" data-image-preview="1" src="${escapeAttr(src)}" alt="${escapeAttr(row.name || "Species")} saved image">
          <div><strong>Saved image available</strong><small>Click the image to preview it. Choose a new file to replace it.</small></div>
        </div>`;
      return;
    }
    box.innerHTML = `<div class="species-image-empty"><strong>No saved image yet.</strong><span>Choose an image file and save this species.</span></div>`;
  }

  function previewSelectedSpeciesImage() {
    const box = $("#speciesCurrentImage");
    const file = $("#speciesImage")?.files?.[0];
    if (!box) return;
    if (!file) { renderSpeciesCurrentImage(state.selectedSpecies); return; }
    releaseImagePreview();
    const url = URL.createObjectURL(file);
    state.selectedImageUrl = url;
    box.innerHTML = `
      <div class="current-image-card pending">
        <img class="previewable-image" data-image-preview="1" src="${escapeAttr(url)}" alt="Selected species image preview">
        <div><strong>New image selected</strong><small>Press Save species to store the file in database/media/species; SQLite keeps only its path.</small></div>
      </div>`;
  }

  function releaseImagePreview() {
    if (state.selectedImageUrl) URL.revokeObjectURL(state.selectedImageUrl);
    state.selectedImageUrl = "";
  }

  function clearSpeciesForm() {
    state.selectedSpecies = null;
    setText("#speciesFormTitle", "New species");
    $("#speciesForm")?.reset();
    setValue("#speciesId", "");
    for (const id of ["#speciesEnvironment", "#speciesRegulation", "#speciesEcology", "#speciesSafety"]) setValue(id, "[]");
    renderSpeciesCurrentImage(null);
  }

  async function saveSpecies(event) {
    event.preventDefault();
    const id = $("#speciesId")?.value || "";
    const form = new FormData();
    form.set("name", $("#speciesName")?.value || "");
    form.set("temperature", $("#speciesTemperature")?.value || "");
    form.set("ph", $("#speciesPh")?.value || "");
    form.set("oxygen", $("#speciesOxygen")?.value || "");
    form.set("turbidity", $("#speciesTurbidity")?.value || "");
    form.set("ammonia", $("#speciesAmmonia")?.value || "");
    form.set("environment", $("#speciesEnvironment")?.value || "[]");
    form.set("regulation", $("#speciesRegulation")?.value || "[]");
    form.set("ecology", $("#speciesEcology")?.value || "[]");
    form.set("safety", $("#speciesSafety")?.value || "[]");
    const image = $("#speciesImage")?.files?.[0];
    if (image) form.set("image", image);
    const result = await apiForm(id ? `/api/admin/species/${encodeURIComponent(id)}` : "/api/admin/species", form, id ? "POST" : "POST");
    if (result.ok) {
      toast("Species saved", result.message || "Species row saved.", "success");
      await loadSpecies();
      await loadLibrary();
      const savedId = result.species?.id || result.id || id;
      if (savedId) await editSpecies(savedId);
      if ($("#speciesImage")) $("#speciesImage").value = "";
    } else {
      toast("Save failed", result.message || "Species was not saved.", "error");
    }
  }

  async function deleteSpecies() {
    const id = $("#speciesId")?.value || "";
    if (!id) return toast("Select species", "Choose a species first.", "warning");
    if (!confirm("Delete this species?")) return;
    const result = await apiJson(`/api/admin/species/${encodeURIComponent(id)}`, "DELETE");
    if (result.ok) {
      clearSpeciesForm();
      await loadSpecies();
      toast("Species deleted", result.message || "Species removed.", "success");
    } else {
      toast("Delete failed", result.message || "Species was not removed.", "error");
    }
  }

  function bindDatabase() {
    $("#reloadDatabaseButton")?.addEventListener("click", loadTables);
    $("#exportTableButton")?.addEventListener("click", () => state.activeTable ? downloadTableCsv(state.activeTable) : toast("Select a table", "Choose the table to export first.", "info"));
    $("#tablePrevButton")?.addEventListener("click", () => { state.tableOffset = Math.max(0, state.tableOffset - tableLimit()); loadTableRows(state.activeTable, true); });
    $("#tableNextButton")?.addEventListener("click", () => { if (state.tableOffset + tableLimit() < state.tableTotal) {state.tableOffset += tableLimit(); loadTableRows(state.activeTable, true);} });
    $("#resetClearDatabaseButton")?.addEventListener("click", resetClearDatabase);
    $("#tableLimit")?.addEventListener("change", () => { state.tableOffset = 0; if (state.activeTable) loadTableRows(state.activeTable, true); });
  }


  async function resetClearDatabase() {
    const message = "This will clear sessions, logs, water readings, predictions, and subscribers. Species will remain. Continue?";
    if (!confirm(message)) return;
    const second = prompt('Type CLEAR to confirm database reset and clear. Species will not be deleted.');
    if (String(second || "").trim().toUpperCase() !== "CLEAR") {
      toast("Reset cancelled", "Confirmation text did not match.", "info");
      return;
    }
    const button = $("#resetClearDatabaseButton");
    setBusy(button, true, "Clearing");
    const result = await apiJson("/api/database/reset-clear", "POST", {});
    setBusy(button, false, "RESET and CLEAR");
    if (result.ok) {
      toast("Database cleared", result.message || "Everything except species was cleared.", "success");
      await loadTables();
      if (state.activeTable) await loadTableRows(state.activeTable);
    } else {
      toast("Clear failed", result.message || "Database was not cleared.", "error");
    }
  }

  async function loadTables() {
    const result = await apiGet("/api/database/tables");
    state.tables = Array.isArray(result.tables) ? result.tables : [];
    renderTablePicker();
  }

  function renderTablePicker() {
    const target = $("#tablePicker");
    if (!target) return;
    if (!state.tables.length) {
      target.innerHTML = `<div class="empty">No tables found.</div>`;
      return;
    }
    target.innerHTML = state.tables.map((name) => `<button class="table-pill ${state.activeTable === name ? "is-active" : ""}" type="button" data-table="${escapeAttr(name)}">${escapeHtml(name)}</button>`).join("");
    target.querySelectorAll("[data-table]").forEach((button) => button.addEventListener("click", () => loadTableRows(button.dataset.table)));
  }

  function tableLimit() { return Math.max(10, Math.min(500, intOr($("#tableLimit")?.value, 100))); }

  async function loadTableRows(name, keepPage = false) {
    if (!name) return;
    if (state.activeTable !== name || !keepPage) state.tableOffset = 0;
    state.activeTable = name;
    renderTablePicker();
    setText("#tableTitle", name.replaceAll("_", " "));
    const limit = tableLimit();
    const result = await apiGet(`/api/database/${encodeURIComponent(name)}?limit=${limit}&offset=${state.tableOffset}`);
    const rows = Array.isArray(result.rows) ? result.rows : [];
    state.tableTotal = Number(result.total || 0);
    renderRows(rows);
    setText("#tableRange", state.tableTotal ? `${state.tableOffset + 1}–${Math.min(state.tableTotal, state.tableOffset + rows.length)} of ${state.tableTotal}` : "0 records");
    const prev = $("#tablePrevButton"), next = $("#tableNextButton");
    if (prev) prev.disabled = state.tableOffset <= 0;
    if (next) next.disabled = state.tableOffset + limit >= state.tableTotal;
  }

  function renderRows(rows) {
    const target = $("#databaseRows");
    if (!target) return;
    if (!rows.length) {
      target.innerHTML = `<div class="empty">No rows found.</div>`;
      return;
    }
    const columns = Array.from(rows.reduce((set, row) => {
      Object.keys(row || {}).forEach((key) => set.add(key));
      return set;
    }, new Set()));
    target.innerHTML = `<table class="table is-hoverable"><thead><tr>${columns.map((col) => `<th>${escapeHtml(col)}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${columns.map((col) => `<td>${state.activeTable === "prediction" && col === "data_path" && row.id && row[col] ? `<a class="file-download" href="/api/prediction/${encodeURIComponent(row.id)}/download" download>Download ZIP ↗</a>` : formatCell(row[col])}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
  }

  async function downloadTableCsv(name) {
    try {
      const response = await fetch(`/api/database/${encodeURIComponent(name)}/export.csv`, {
        credentials: "same-origin", headers: state.token ? { "X-AquaCulture-Admin-Token": state.token } : {},
      });
      if (!response.ok) throw new Error(`Export unavailable (${response.status}).`);
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `aquaculture_${name}.csv`;
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
      toast("Export ready", `${name.replaceAll("_", " ")} downloaded as CSV.`, "success");
    } catch (error) {
      toast("Export failed", error.message || "Please try again.", "error");
    }
  }

  function bindLibrary() {
    $("#reloadLibraryButton")?.addEventListener("click", loadLibrary);
    $("#librarySearch")?.addEventListener("input", renderLibraryFiles);
  }

  async function loadLibrary() {
    const requestId = ++state.libraryRequest;
    const result = await apiGet(`/api/admin/library?folder=${encodeURIComponent(state.libraryFolder)}`);
    if (requestId !== state.libraryRequest) return;
    if (!result.ok) {
      setText("#librarySummary", "Library unavailable");
      setText("#libraryFiles", result.message || "Could not load files. Refresh to try again.");
      return;
    }
    state.libraryFolder = result.folder || state.libraryFolder;
    state.libraryFolders = Array.isArray(result.folders) ? result.folders : [];
    state.libraryFiles = Array.isArray(result.files) ? result.files : [];
    setText("#librarySummary", `${result.total_files || 0} files · ${formatBytes(result.total_bytes)}`);
    const folders = $("#libraryFolders");
    if (folders) {
      folders.innerHTML = state.libraryFolders.map((folder) => `
        <button class="library-folder ${folder.id === state.libraryFolder ? "is-active" : ""}" type="button" data-library-folder="${escapeAttr(folder.id)}" ${folder.id === state.libraryFolder ? 'aria-current="true"' : ""}>
          <span class="library-folder-icon" aria-hidden="true">${folder.kind === "image" ? "▧" : folder.kind === "zip" ? "▤" : "▱"}</span>
          <span><strong>${escapeHtml(folder.label || folder.name)}</strong><small>${escapeHtml(folder.file_count)} files · ${formatBytes(folder.total_bytes)}</small></span>
        </button>`).join("");
      folders.querySelectorAll("[data-library-folder]").forEach((button) => button.addEventListener("click", () => {
        state.libraryFolder = button.dataset.libraryFolder;
        setValue("#librarySearch", "");
        setText("#libraryFiles", "Loading files…");
        loadLibrary();
      }));
    }
    const selected = state.libraryFolders.find((folder) => folder.id === state.libraryFolder);
    setText("#libraryTitle", selected?.label || state.libraryFolder);
    setText("#libraryPath", `database/${selected?.path || "media"}${selected?.legacy ? " · Older generated files; new uploads use one original image." : ""}`);
    renderLibraryFiles();
  }

  function renderLibraryFiles() {
    const target = $("#libraryFiles");
    if (!target) return;
    const query = String($("#librarySearch")?.value || "").trim().toLowerCase();
    const files = state.libraryFiles.filter((file) => String(file.path || file.name).toLowerCase().includes(query));
    if (!files.length) {
      target.innerHTML = `<div class="empty">${query ? "No matching files." : "This folder has no files yet."}</div>`;
      return;
    }
    target.innerHTML = files.map((file) => `
      <article class="library-file">
        <div class="library-file-preview">${file.previewable
          ? `<img src="${escapeAttr(file.url)}" alt="${escapeAttr(file.name)}" loading="lazy" class="previewable-image" data-image-preview="1">`
          : `<span class="library-file-icon" aria-hidden="true">${file.kind === "zip" ? "ZIP" : "FILE"}</span>`}</div>
        <div class="library-file-meta"><strong>${escapeHtml(file.name)}</strong><small>${formatBytes(file.size_bytes ?? file.size)} · ${escapeHtml(formatFileDate(file.modified_at))}</small></div>
        <a class="button is-info is-light is-small" href="${escapeAttr(file.download_url)}" download>Download</a>
      </article>`).join("");
  }

  function formatBytes(value) {
    const bytes = Math.max(0, Number(value) || 0);
    const units = ["B", "KB", "MB", "GB"];
    const unit = Math.min(units.length - 1, Math.floor(Math.log(bytes || 1) / Math.log(1024)));
    return `${(bytes / (1024 ** unit)).toLocaleString(undefined, { maximumFractionDigits: unit ? 1 : 0 })} ${units[unit]}`;
  }

  function formatFileDate(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "Unknown date" : date.toLocaleDateString();
  }

  function bindAccess() {
    $("#recoveryEmailForm")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const result = await apiJson("/api/admin/recovery-email", "PATCH", { email: $("#adminRecoveryEmail").value, current_password: $("#recoveryCurrentPassword").value });
      $("#recoveryCurrentPassword").value = "";
      toast(result.ok ? "Recovery email saved" : "Save failed", result.message, result.ok ? "success" : "error");
    });
    $("#passwordForm")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const current_password = $("#currentPassword")?.value || "";
      const new_password = $("#newPassword")?.value || "";
      const result = await apiJson("/api/admin/password", "PATCH", { current_password, new_password });
      $("#currentPassword").value = "";
      $("#newPassword").value = "";
      if (result.ok) {
        $("#passwordForm")?.reset();
        toast("Password updated", result.message || "Admin password updated.", "success");
        leaveSettings();
      } else {
        toast("Update failed", result.message || "Password was not updated.", "error");
      }
    });
  }

  async function logout() {
    const result = await apiJson("/api/admin/logout", "POST", {});
    if (!result.ok && result.status !== 401) {
      toast("Logout failed", result.message || "Could not end the admin session. Please try again.", "error");
      return;
    }
    localStorage.removeItem("aquaculture_admin_token");
    localStorage.removeItem("aquaculture_admin_session");
    toast("Logged out", "Settings are locked.", "success");
    window.location.replace("/");
  }

  async function apiGet(url) {
    return apiJson(url, "GET");
  }

  async function apiJson(url, method = "GET", body = undefined) {
    try {
      const options = { method, headers: { "Accept": "application/json" }, credentials: "same-origin" };
      if (state.token) options.headers["X-AquaCulture-Admin-Token"] = state.token;
      if (body !== undefined && method !== "GET") {
        options.headers["Content-Type"] = "application/json";
        options.body = JSON.stringify(body);
      }
      const response = await fetch(url, options);
      const text = await response.text();
      let data = {};
      try { data = text ? JSON.parse(text) : {}; } catch (_) { data = { ok: false, message: text || response.statusText }; }
      data.status = response.status;
      if (response.status === 401) {
        toast("Session required", data.message || "Please login again.", "warning");
        leaveSettings();
      }
      return data;
    } catch (error) {
      return { ok: false, message: error.message || "Network error" };
    }
  }

  async function apiForm(url, form, method = "POST") {
    try {
      const headers = { "Accept": "application/json" };
      if (state.token) headers["X-AquaCulture-Admin-Token"] = state.token;
      const response = await fetch(url, { method, headers, body: form, credentials: "same-origin" });
      const data = await response.json().catch(() => ({ ok: false, message: response.statusText }));
      data.status = response.status;
      if (response.status === 401) leaveSettings();
      return data;
    } catch (error) {
      return { ok: false, message: error.message || "Network error" };
    }
  }

  function openModal(id) {
    const modal = document.getElementById(id);
    if (modal?.showModal) modal.showModal();
  }

  function closeModal(id) {
    const modal = document.getElementById(id);
    if (modal?.close) modal.close();
  }

  function toast(title, message = "", type = "info") {
    if (window.AquaToast?.show) return window.AquaToast.show({ title, message, type });
    if (typeof window.showToast === "function") return window.showToast(`${title}${message ? `: ${message}` : ""}`, type);
    console.log(title, message, type);
  }

  function setBusy(button, busy, text) {
    if (!button) return;
    button.disabled = Boolean(busy);
    if (text) button.textContent = text;
  }

  function setText(selector, value) {
    const el = $(selector);
    if (el) el.textContent = String(value ?? "");
  }

  function setValue(selector, value) {
    const el = $(selector);
    if (el) el.value = String(value ?? "");
  }

  function numberOr(value, fallback) {
    const number = Number.parseFloat(value);
    return Number.isFinite(number) ? number : fallback;
  }

  function intOr(value, fallback) {
    const number = Number.parseInt(value, 10);
    return Number.isFinite(number) ? number : fallback;
  }

  function formatCell(value) {
    if (value === null || value === undefined) return "--";
    if (typeof value === "object") {
      if (value.type === "BLOB") return `<span class="status-chip">BLOB · ${escapeHtml(value.bytes || 0)} bytes</span>`;
      return `<code>${escapeHtml(JSON.stringify(value))}</code>`;
    }
    const text = String(value);
    return escapeHtml(text.length > 180 ? `${text.slice(0, 180)}…` : text);
  }

  function shortText(value, max = 90) {
    const text = String(value ?? "").replace(/\s+/g, " ").trim();
    return text.length > max ? `${text.slice(0, max)}…` : text;
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
    return escapeHtml(value);
  }
})();
