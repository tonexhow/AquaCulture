(() => {
  'use strict';

  const state = {
    activePanel: 'wirelessPanel',
    status: null,
    selectedWiredMode: null,
    savedNetworks: [],
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  const els = {
    header: $('#top'),
    hamburger: $('#hamburgerButton'),
    menu: $('#networkMenu'),
    navButtons: $$('[data-panel]'),
    panels: $$('[data-page-panel]'),
    statusDot: $('#statusDot'),
    statusTitle: $('#statusTitle'),
    statusText: $('#statusText'),
    wirelessInterface: $('#wirelessInterface'),
    wirelessState: $('#wirelessState'),
    savedList: $('#savedList'),
    networkList: $('#networkList'),
    scanProgress: $('#scanProgress'),
    ssidInput: $('#ssidInput'),
    passwordInput: $('#passwordInput'),
    hiddenNetworkInput: $('#hiddenNetworkInput'),
    ethernetDevices: $('#ethernetDevices'),
    usbTetherDevices: $('#usbTetherDevices'),
    wiredConfirmBox: $('#wiredConfirmBox'),
    confirmTitle: $('#confirmTitle'),
    confirmText: $('#confirmText'),
    hotspotName: $('#hotspotName'),
    hotspotNote: $('#hotspotNote'),
    footerHotspotName: $('#footerHotspotName'),
    hotspotInput: $('#hotspotInput'),
    editModal: $('#editSavedModal'),
    editOriginalName: $('#editOriginalName'),
    editNameInput: $('#editNameInput'),
    editPasswordInput: $('#editPasswordInput'),
    editAutoconnectInput: $('#editAutoconnectInput'),
  };

  document.addEventListener('DOMContentLoaded', init);

  function init() {
    bindNavigation();
    bindActions();
    refreshAll();
  }

  function bindNavigation() {
    els.hamburger?.addEventListener('click', () => {
      const open = !els.header.classList.contains('menu-open');
      els.header.classList.toggle('menu-open', open);
      els.hamburger.setAttribute('aria-expanded', String(open));
    });

    els.navButtons.forEach((button) => {
      button.addEventListener('click', () => {
        showPanel(button.dataset.panel);
        closeMobileMenu();
      });
    });
  }

  function bindActions() {
    $('#refreshWirelessButton')?.addEventListener('click', refreshWireless);
    $('#refreshWiredButton')?.addEventListener('click', refreshWired);
    $('#refreshLocalButton')?.addEventListener('click', refreshStatus);
    $('#reloadSavedButton')?.addEventListener('click', loadSavedNetworks);
    $('#scanButton')?.addEventListener('click', scanNetworks);
    $('#connectButton')?.addEventListener('click', connectManualNetwork);
    $('#goLiveButton')?.addEventListener('click', goLive);
    $('#saveHotspotButton')?.addEventListener('click', saveHotspotName);
    $('#saveSavedNetworkButton')?.addEventListener('click', saveSavedNetworkChanges);

    $$('[data-select-wired]').forEach((button) => {
      button.addEventListener('click', () => selectWiredMode(button.dataset.selectWired));
    });

    $$('[data-close-modal]').forEach((button) => button.addEventListener('click', closeEditModal));
  }

  function showPanel(panelId) {
    if (!panelId) return;
    state.activePanel = panelId;
    els.panels.forEach((panel) => panel.classList.toggle('active-panel', panel.id === panelId));
    els.navButtons.forEach((button) => button.classList.toggle('is-active', button.dataset.panel === panelId));
  }

  function closeMobileMenu() {
    els.header?.classList.remove('menu-open');
    els.hamburger?.setAttribute('aria-expanded', 'false');
  }

  async function refreshAll() {
    await refreshStatus();
    await loadSavedNetworks();
  }

  async function refreshWireless() {
    await refreshStatus();
    await loadSavedNetworks();
  }

  async function refreshWired() {
    await refreshStatus();
    if (state.selectedWiredMode) renderWiredConfirm(state.selectedWiredMode);
  }

  async function refreshStatus() {
    const response = await apiGet('/api/network/status');
    if (!response.ok) {
      setStatus(false, 'Network unavailable', response.error || 'Could not read network status.');
      return;
    }

    state.status = response.data || {};
    const ready = !!(state.status.network_tools_available || state.status.nmcli_available || state.status.windows_netsh_available);
    const toolName = state.status.network_tool || (state.status.nmcli_available ? 'NetworkManager' : 'OS tools');
    setStatus(ready, ready ? `${toolName} ready` : 'Network tools unavailable', state.status.message || 'Ready');
    renderWirelessStatus();
    renderWiredDevices();
    renderHotspot();
  }

  function setStatus(ok, title, text) {
    els.statusDot?.classList.toggle('ok', !!ok);
    if (els.statusTitle) els.statusTitle.textContent = title;
    if (els.statusText) els.statusText.textContent = text;
  }

  function renderWirelessStatus() {
    const wireless = state.status?.wireless || {};
    const first = (wireless.interfaces || [])[0];
    const active = wireless.active || first;
    if (els.wirelessInterface) els.wirelessInterface.textContent = active?.device || 'No WLAN adapter detected';
    if (els.wirelessState) {
      const connection = active?.connection ? ` · ${active.connection}` : '';
      els.wirelessState.textContent = active ? `${active.state || 'unknown'}${connection}` : 'No wireless interface detected by this OS';
    }
  }

  async function loadSavedNetworks() {
    renderLoading(els.savedList, 'Loading saved networks…');
    const response = await apiGet('/api/network/saved');
    if (!response.ok) {
      renderEmpty(els.savedList, response.error || response.message || 'Saved networks unavailable.');
      return;
    }

    state.savedNetworks = response.data?.saved || [];
    if (!state.savedNetworks.length) {
      renderEmpty(els.savedList, 'No saved Wi‑Fi networks were returned by the OS.');
      return;
    }

    els.savedList.innerHTML = state.savedNetworks.map((item, index) => `
      <article class="saved-item">
        <div class="saved-main">
          <button type="button" data-connect-saved="${escapeAttr(item.name)}" title="Connect ${escapeAttr(item.ssid || item.name)}">
            ${escapeHtml(item.ssid || item.name)}
          </button>
          <span class="badge ${item.connected ? '' : 'muted'}">${item.connected ? 'Connected' : 'Saved'}</span>
        </div>
        <div class="item-actions">
          <button class="btn btn-small btn-soft" type="button" data-connect-saved="${escapeAttr(item.name)}">Connect</button>
          <button class="btn btn-small btn-soft" type="button" data-modify-saved="${index}">Modify</button>
          <button class="btn btn-small btn-soft" type="button" data-forget-saved="${escapeAttr(item.name)}">Forget</button>
        </div>
      </article>
    `).join('');

    $$('[data-connect-saved]', els.savedList).forEach((button) => {
      button.addEventListener('click', (event) => {
        event.stopPropagation();
        connectSavedNetwork(button.dataset.connectSaved);
      });
    });
    $$('[data-modify-saved]', els.savedList).forEach((button) => {
      button.addEventListener('click', () => openEditModal(state.savedNetworks[Number(button.dataset.modifySaved)]));
    });
    $$('[data-forget-saved]', els.savedList).forEach((button) => {
      button.addEventListener('click', () => forgetSavedNetwork(button.dataset.forgetSaved));
    });
  }

  async function scanNetworks() {
    const interfaceName = getWirelessInterface();
    els.scanProgress.hidden = false;
    renderEmpty(els.networkList, 'Scanning…');

    const response = await apiPost('/api/network/scan', { interface: interfaceName });
    els.scanProgress.hidden = true;

    if (!response.ok) {
      renderEmpty(els.networkList, response.error || response.message || 'Wi‑Fi scan failed.');
      toast('Scan failed', response.error || response.message || 'No scan result was returned.', 'bad');
      return;
    }

    const networks = response.data?.networks || [];
    if (!networks.length) {
      renderEmpty(els.networkList, 'The OS returned no nearby Wi‑Fi networks.');
      toast('Scan complete', 'No nearby networks were returned.', 'ok');
      return;
    }

    els.networkList.innerHTML = networks.map((item) => `
      <article class="network-item">
        <div class="network-main-row">
          <button type="button" data-pick-ssid="${escapeAttr(item.ssid)}">${escapeHtml(item.ssid)}</button>
          <span class="badge ${item.in_use ? '' : 'muted'}">${item.in_use ? 'In use' : `${Number(item.signal || 0)}%`}</span>
        </div>
        <div class="signal-bar" aria-hidden="true"><span style="--signal: ${Number(item.signal || 0)}%"></span></div>
        <div class="item-actions">
          <span class="badge muted">${escapeHtml(item.security || 'Open')}</span>
          ${item.saved ? '<span class="badge">Saved</span>' : ''}
          <button class="btn btn-small btn-soft" type="button" data-pick-ssid="${escapeAttr(item.ssid)}">Use SSID</button>
        </div>
      </article>
    `).join('');

    $$('[data-pick-ssid]', els.networkList).forEach((button) => {
      button.addEventListener('click', () => {
        els.ssidInput.value = button.dataset.pickSsid || '';
        els.passwordInput.focus();
      });
    });

    toast('Scan complete', `${networks.length} real network${networks.length === 1 ? '' : 's'} found.`, 'ok');
  }

  async function connectManualNetwork() {
    const ssid = els.ssidInput.value.trim();
    const password = els.passwordInput.value;
    const hidden = els.hiddenNetworkInput.checked;
    if (!ssid) {
      toast('Missing SSID', 'Enter or select a Wi‑Fi name first.', 'bad');
      return;
    }

    setButtonBusy($('#connectButton'), true, 'Connecting…');
    const response = await apiPost('/api/network/connect', { ssid, password, hidden, interface: getWirelessInterface() });
    setButtonBusy($('#connectButton'), false, 'Connect');

    if (!response.ok) {
      toast('Connection failed', response.error || response.message || 'Could not connect.', 'bad');
      return;
    }

    toast('Connected', response.message || `Connected to ${ssid}.`, 'ok');
    await refreshWireless();
  }

  async function connectSavedNetwork(name) {
    if (!name) return;
    const response = await apiPost('/api/network/saved/connect', { name, interface: getWirelessInterface() });
    if (!response.ok) {
      toast('Connection failed', response.error || response.message || 'Could not connect saved network.', 'bad');
      return;
    }
    toast('Connected', response.message || `${name} connected.`, 'ok');
    await refreshWireless();
  }

  async function forgetSavedNetwork(name) {
    if (!name) return;
    if (!window.confirm(`Forget saved Wi‑Fi network "${name}"?`)) return;
    const response = await apiPost('/api/network/forget', { name });
    if (!response.ok) {
      toast('Forget failed', response.error || response.message || 'Could not forget network.', 'bad');
      return;
    }
    toast('Forgot network', response.message || `${name} was removed.`, 'ok');
    await loadSavedNetworks();
  }

  function openEditModal(item) {
    if (!item) return;
    els.editOriginalName.value = item.name || item.ssid || '';
    els.editNameInput.value = item.name || item.ssid || '';
    els.editPasswordInput.value = '';
    els.editAutoconnectInput.checked = !!item.autoconnect;
    if (typeof els.editModal.showModal === 'function') els.editModal.showModal();
    else els.editModal.setAttribute('open', '');
  }

  function closeEditModal() {
    if (typeof els.editModal.close === 'function') els.editModal.close();
    else els.editModal.removeAttribute('open');
  }

  async function saveSavedNetworkChanges() {
    const name = els.editOriginalName.value.trim();
    const newName = els.editNameInput.value.trim();
    const password = els.editPasswordInput.value;
    const autoconnect = els.editAutoconnectInput.checked;
    const response = await apiPost('/api/network/modify', { name, new_name: newName, password, autoconnect });
    if (!response.ok) {
      toast('Update failed', response.error || response.message || 'Could not update saved network.', 'bad');
      return;
    }
    closeEditModal();
    toast('Network updated', response.message || 'Saved network updated.', 'ok');
    await loadSavedNetworks();
  }

  function renderWiredDevices() {
    const wired = state.status?.wired || {};
    renderDeviceList(els.ethernetDevices, wired.ethernet || [], 'No Ethernet device detected.');
    renderDeviceList(els.usbTetherDevices, wired.usb_tether || [], 'No USB tether device detected.');
  }

  function renderDeviceList(container, devices, emptyText) {
    if (!container) return;
    if (!devices.length) return renderEmpty(container, emptyText);
    container.innerHTML = devices.map((item) => `
      <div class="device-item">
        <div class="device-main">
          <strong>${escapeHtml(item.device || 'device')}</strong>
          <span class="badge ${item.connected ? '' : 'muted'}">${escapeHtml(item.state || 'unknown')}</span>
        </div>
        <small>${escapeHtml(item.connection || 'No active connection')}</small>
      </div>
    `).join('');
  }

  async function selectWiredMode(mode) {
    state.selectedWiredMode = mode;
    $$('[data-wired-mode]').forEach((card) => card.classList.toggle('is-selected', card.dataset.wiredMode === mode));
    els.wiredConfirmBox.hidden = false;
    els.confirmTitle.textContent = mode === 'ethernet' ? 'Ethernet selected' : 'USB Tether selected';
    els.confirmText.textContent = 'Checking real device status. Press Go Live after the device is detected.';

    const response = await apiPost('/api/network/mode', { mode });
    if (!response.ok) {
      toast('Mode failed', response.error || response.message || 'Could not select mode.', 'bad');
      return;
    }
    await refreshStatus();
    renderWiredConfirm(mode);
  }

  function renderWiredConfirm(mode) {
    const wired = state.status?.wired || {};
    const devices = mode === 'ethernet' ? wired.ethernet || [] : wired.usb_tether || [];
    els.wiredConfirmBox.hidden = false;
    els.confirmTitle.textContent = mode === 'ethernet' ? 'Ethernet selected' : 'USB Tether selected';
    els.confirmText.textContent = devices.length
      ? `${devices.length} matching device${devices.length === 1 ? '' : 's'} detected. Press Go Live to activate it and stop the local hotspot.`
      : 'No matching device detected yet. Connect the cable/tether, then press Refresh.';
  }

  async function goLive() {
    const mode = state.selectedWiredMode;
    if (!mode) return toast('Choose wired path', 'Choose Ethernet or USB Tether first.', 'bad');
    if (!window.confirm('Go Live may stop the local hotspot and disconnect this page. Continue?')) return;

    setButtonBusy($('#goLiveButton'), true, 'Going live…');
    const response = await apiPost('/api/network/go-live', { mode });
    setButtonBusy($('#goLiveButton'), false, 'Go Live');
    if (!response.ok) return toast('Go Live failed', response.error || response.message || 'Could not activate wired path.', 'bad');
    toast('Go Live complete', response.message || 'Wired path activated.', 'ok');
    await refreshStatus();
  }

  function renderHotspot() {
    const hotspot = state.status?.hotspot || {};
    const name = hotspot.name || 'AquaCulture-Setup';
    if (els.hotspotName) els.hotspotName.textContent = name;
    if (els.footerHotspotName) els.footerHotspotName.textContent = name;
    if (els.hotspotInput && !els.hotspotInput.value) els.hotspotInput.value = name;
    if (els.hotspotNote) {
      els.hotspotNote.textContent = hotspot.applied
        ? 'Applied to the real NetworkManager hotspot configuration.'
        : 'Saved locally or waiting for Raspberry Pi hotspot configuration.';
    }
  }

  async function saveHotspotName() {
    const name = els.hotspotInput.value.trim();
    if (!name) return toast('Missing hotspot name', 'Enter a hotspot name first.', 'bad');
    setButtonBusy($('#saveHotspotButton'), true, 'Applying…');
    const response = await apiPost('/api/network/hotspot', { name });
    setButtonBusy($('#saveHotspotButton'), false, 'Save and Apply');
    if (!response.ok) {
      toast('Hotspot update failed', response.error || response.message || 'Could not apply hotspot name.', 'bad');
      if (response.details?.hotspot) {
        state.status = { ...(state.status || {}), hotspot: response.details.hotspot };
        renderHotspot();
      }
      return;
    }
    toast('Hotspot updated', response.message || 'Hotspot name applied.', 'ok');
    await refreshStatus();
  }

  function getWirelessInterface() {
    const wireless = state.status?.wireless || {};
    const first = (wireless.interfaces || [])[0];
    return first?.device || 'wlan0';
  }

  async function apiGet(url) {
    try {
      const response = await fetch(url, { headers: { Accept: 'application/json' } });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) return { ok: false, ...(data || {}), status: response.status };
      return data;
    } catch (error) {
      return { ok: false, error: error.message || String(error) };
    }
  }

  async function apiPost(url, body = {}) {
    try {
      const response = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) return { ok: false, ...(data || {}), status: response.status };
      return data;
    } catch (error) {
      return { ok: false, error: error.message || String(error) };
    }
  }

  function renderLoading(container, text) {
    if (!container) return;
    container.innerHTML = `<div class="empty-state"><div class="spinner" aria-hidden="true"></div><span>${escapeHtml(text)}</span></div>`;
  }

  function renderEmpty(container, text) {
    if (!container) return;
    container.innerHTML = `<div class="empty-state">${escapeHtml(text)}</div>`;
  }

  function setButtonBusy(button, busy, label) {
    if (!button) return;
    button.disabled = !!busy;
    button.textContent = label;
  }

  function toast(title, message, type = 'ok') {
    if (window.AquaToast?.show) return window.AquaToast.show(title, message, type);
    console[type === 'bad' ? 'error' : 'log'](`${title}: ${message}`);
  }

  function escapeHtml(value) {
    return String(value ?? '')
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#039;');
  }

  function escapeAttr(value) {
    return escapeHtml(value).replaceAll('`', '&#096;');
  }
})();
