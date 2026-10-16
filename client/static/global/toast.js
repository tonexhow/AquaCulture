/* Shared AquaCulture notifications (no external dependencies). */
(() => {
  'use strict';
  if (window.AquaToast) return;
  const TYPES = new Set(['success', 'warning', 'error', 'info', 'ok', 'bad']);
  const escapeHtml = (value) => String(value ?? '').replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;').replaceAll("'", '&#039;');

  function show(title, messageOrType = '', legacyType = '') {
    // Settings uses show({title,message,type}); Home/Network use positional arguments.
    if (title && typeof title === 'object') {
      const options = title;
      title = options.title || 'Notification';
      messageOrType = options.message || '';
      legacyType = options.type || 'info';
    }
    let message = messageOrType;
    let type = legacyType || 'info';
    if (!legacyType && TYPES.has(messageOrType)) { type = messageOrType; message = ''; }
    const normalized = type === 'bad' ? 'error' : type === 'ok' ? 'success' : type;
    let stack = document.querySelector('.toast-stack');
    if (!stack) {
      stack = document.createElement('div');
      stack.className = 'toast-stack';
      stack.setAttribute('aria-label', 'Notifications');
      stack.setAttribute('aria-live', 'polite');
      document.body.append(stack);
    }
    while (stack.children.length >= 3) stack.firstElementChild.remove();
    const item = document.createElement('div');
    item.className = `toast ${TYPES.has(normalized) ? normalized : 'info'}`;
    item.setAttribute('role', normalized === 'error' ? 'alert' : 'status');
    item.innerHTML = `<strong>${escapeHtml(title)}</strong>${message ? `<small>${escapeHtml(message)}</small>` : ''}
      <button type="button" class="toast__close" aria-label="Dismiss notification">×</button>`;
    stack.append(item);
    let timer;
    const dismiss = () => {
      if (!item.isConnected || item.classList.contains('is-leaving')) return;
      clearTimeout(timer);
      item.classList.add('is-leaving');
      window.setTimeout(() => item.remove(), 160);
    };
    const start = () => { clearTimeout(timer); timer = window.setTimeout(dismiss, 4800); };
    item.querySelector('.toast__close').addEventListener('click', dismiss);
    item.addEventListener('mouseenter', () => clearTimeout(timer));
    item.addEventListener('mouseleave', start);
    item.addEventListener('focusin', () => clearTimeout(timer));
    item.addEventListener('focusout', start);
    start();
    return item;
  }
  window.AquaToast = { show };
})();
