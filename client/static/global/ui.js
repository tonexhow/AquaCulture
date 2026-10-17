/* Shared theme, password visibility and viewport-aware tooltips. */
(() => {
  "use strict";

  const root = document.documentElement;
  let passwordSequence = 0;
  let tooltip;
  let tooltipTarget;
  let previousDescription = null;

  function applyTheme(theme) {
    let saved;
    try { saved = localStorage.getItem("aquaculture_theme"); } catch (_) { /* storage may be unavailable */ }
    root.dataset.theme = theme || (saved === "light" || saved === "dark" ? saved
      : window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    syncThemeAssets();
  }

  function syncThemeAssets() {
    const theme = root.dataset.theme === "dark" ? "dark" : "light";
    document.querySelectorAll("[data-theme-logo]").forEach((node) => {
      const attr = node.tagName === "LINK" ? "href" : "src";
      const current = node.getAttribute(attr) || "";
      const next = current.replace(/logo(?:-light|-dark)?\.svg(?:\?.*)?$/, `logo-${theme}.svg`);
      if (current !== next) node.setAttribute(attr, next);
    });
    document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
      const label = `Switch to ${theme === "dark" ? "light" : "dark"} theme`;
      button.setAttribute("aria-label", label);
      button.dataset.tooltip = label;
      button.textContent = theme === "dark" ? "☀" : "◐";
    });
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", theme === "dark" ? "#0d1820" : "#f5f8f8");
    if (tooltipTarget) hideTooltip();
  }

  const eye = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/><path class="aq-eye-slash" d="m3 3 18 18"/></svg>';

  function enhancePasswords() {
    document.querySelectorAll('input[type="password"]:not([data-password-toggle])').forEach((input) => {
      input.dataset.passwordToggle = "1";
      if (!input.id) input.id = `aq-password-${++passwordSequence}`;
      let wrapper = input.closest(".password-wrap");
      if (!wrapper) {
        wrapper = document.createElement("span");
        input.before(wrapper);
        wrapper.append(input);
      }
      wrapper.classList.add("aq-password-control");
      const button = wrapper.querySelector(".eye-btn") || document.createElement("button");
      button.type = "button";
      button.className = "aq-password-toggle";
      button.innerHTML = eye;
      button.setAttribute("aria-controls", input.id);
      button.setAttribute("aria-label", "Show password");
      button.setAttribute("aria-pressed", "false");
      button.dataset.tooltip = "Show password";
      if (!button.parentNode) wrapper.append(button);
      button.addEventListener("click", () => {
        const visible = input.type === "password";
        input.type = visible ? "text" : "password";
        button.dataset.visible = String(visible);
        button.setAttribute("aria-pressed", String(visible));
        button.setAttribute("aria-label", visible ? "Hide password" : "Show password");
        button.dataset.tooltip = visible ? "Hide password" : "Show password";
        hideTooltip();
      });
      input.form?.addEventListener("reset", () => {
        input.type = "password";
        button.dataset.visible = "false";
        button.setAttribute("aria-pressed", "false");
        button.setAttribute("aria-label", "Show password");
        button.dataset.tooltip = "Show password";
      });
    });
  }

  function hideTooltip() {
    if (!tooltip || !tooltipTarget) return;
    if (previousDescription === null) tooltipTarget.removeAttribute("aria-describedby");
    else tooltipTarget.setAttribute("aria-describedby", previousDescription);
    tooltipTarget = null;
    if (tooltip.hasAttribute("popover") && tooltip.matches(":popover-open")) tooltip.hidePopover();
    tooltip.hidden = true;
  }

  function showTooltip(target) {
    if (!target || !target.dataset.tooltip?.trim() || target.disabled) return;
    hideTooltip();
    if (!tooltip) {
      tooltip = document.createElement("div");
      tooltip.id = "aq-tooltip";
      tooltip.className = "aq-tooltip";
      tooltip.setAttribute("role", "tooltip");
      if (typeof tooltip.showPopover === "function") tooltip.setAttribute("popover", "manual");
      document.body.append(tooltip);
    }
    target.removeAttribute("title");
    tooltip.textContent = target.dataset.tooltip;
    tooltip.hidden = false;
    if (tooltip.hasAttribute("popover")) tooltip.showPopover();
    tooltipTarget = target;
    previousDescription = target.getAttribute("aria-describedby");
    target.setAttribute("aria-describedby", [previousDescription, tooltip.id].filter(Boolean).join(" "));
    const anchor = target.getBoundingClientRect();
    const bounds = tooltip.getBoundingClientRect();
    const left = Math.max(12, Math.min(anchor.left + (anchor.width - bounds.width) / 2, window.innerWidth - bounds.width - 12));
    const above = anchor.top - bounds.height - 8;
    const top = above >= 12 ? above : Math.min(anchor.bottom + 8, window.innerHeight - bounds.height - 12);
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${Math.max(12, top)}px`;
  }

  function init() {
    syncThemeAssets();
    enhancePasswords();
    new MutationObserver(enhancePasswords).observe(document.body, { childList: true, subtree: true });
    new MutationObserver(syncThemeAssets).observe(root, { attributes: true, attributeFilter: ["data-theme"] });
    document.addEventListener("click", (event) => {
      if (!event.target.closest("[data-theme-toggle]")) return;
      const next = root.dataset.theme === "dark" ? "light" : "dark";
      try { localStorage.setItem("aquaculture_theme", next); } catch (_) { /* still allow a temporary theme */ }
      applyTheme(next);
    });
    document.addEventListener("pointerover", (event) => {
      if (event.pointerType === "touch") return;
      const target = event.target.closest("[data-tooltip]");
      if (target && !target.contains(event.relatedTarget)) showTooltip(target);
    });
    document.addEventListener("pointerout", (event) => {
      if (tooltipTarget?.contains(event.target) && !tooltipTarget.contains(event.relatedTarget)) hideTooltip();
    });
    document.addEventListener("focusin", (event) => showTooltip(event.target.closest("[data-tooltip]")));
    document.addEventListener("focusout", hideTooltip);
    document.addEventListener("keydown", (event) => { if (event.key === "Escape") hideTooltip(); });
    document.addEventListener("scroll", hideTooltip, true);
    window.addEventListener("resize", hideTooltip);
    window.addEventListener("storage", (event) => { if (event.key === "aquaculture_theme") applyTheme(); });
  }

  window.AquaUI = { applyTheme };
  applyTheme();
  document.addEventListener("DOMContentLoaded", init);
})();
