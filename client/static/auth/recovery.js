(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const resetting = document.body.dataset.mode === "reset";
  let token = resetting ? new URLSearchParams(location.search).get("token") : null;
  // Remove the token from browser history before any other application requests.
  if (resetting) history.replaceState(null, "", location.pathname);
  document.querySelectorAll("[data-eye]").forEach((button) => button.addEventListener("click", () => {
    const input = $(button.dataset.eye), visible = input.type === "password";
    input.type = visible ? "text" : "password";
    button.setAttribute("aria-pressed", String(visible));
    button.setAttribute("aria-label", `${visible ? "Hide" : "Show"} ${button.dataset.eye === "newPassword" ? "new" : "confirmation"} password`);
  }));
  if (resetting && !token) { $("recoveryMessage").textContent = "This reset link is invalid. Request a new link."; $("submitRecovery").disabled = true; }
  let busy = false;
  $("recoveryForm").addEventListener("submit", async (event) => {
    event.preventDefault(); if (busy) return;
    if (resetting && $("newPassword").value !== $("confirmPassword").value) { $("recoveryMessage").textContent = "Passwords do not match."; return; }
    busy = true; $("submitRecovery").disabled = true; $("recoveryMessage").textContent = "Please wait…";
    let payload = resetting ? { token, new_password: $("newPassword").value, confirm_password: $("confirmPassword").value } : { email: $("recoveryEmail").value };
    if (resetting) { $("newPassword").value = ""; $("confirmPassword").value = ""; }
    try {
      const response = await fetch(resetting ? "/api/admin/password/reset" : "/api/admin/password/forgot", { method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin", cache: "no-store", referrerPolicy: "no-referrer", body: JSON.stringify(payload) });
      const data = await response.json();
      $("recoveryMessage").textContent = data.message + (data.retry_after_seconds ? ` Try again in ${data.retry_after_seconds} seconds.` : "");
      if (resetting && data.ok) { token = null; $("recoveryForm").hidden = true; localStorage.removeItem("aquaculture_admin_token"); localStorage.removeItem("aquaculture_admin_session"); }
    } catch { $("recoveryMessage").textContent = "Unable to reach AquaCulture. Please try again."; }
    finally { payload = null; busy = false; $("submitRecovery").disabled = false; }
  });
})();
