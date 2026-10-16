import "./version.js";
const { version: APP_VERSION, cacheId: CACHE_ID } = globalThis.AQUACULTURE_VERSION;

export class OfflineAccess extends EventTarget {
  constructor() { super(); this.registration = null; this.status = { state: "NOT_INSTALLED", ready: false, cacheId: CACHE_ID, version: null, updatedAt: null }; }
  report(value) { this.status = { ...this.status, ...value }; this.dispatchEvent(new CustomEvent("change", { detail: this.status })); return this.status; }
  supported() { return Boolean(window.isSecureContext && "serviceWorker" in navigator && "caches" in globalThis && "MessageChannel" in globalThis); }
  async message(worker, type) {
    if (!worker) throw new Error("No active service worker controls this page.");
    const channel = new MessageChannel();
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { channel.port1.close(); reject(new Error("Offline verification timed out.")); }, 8000);
      channel.port1.onmessage = ({ data }) => { clearTimeout(timer); channel.port1.close(); resolve(data); };
      try { worker.postMessage({ type }, [channel.port2]); }
      catch (error) { clearTimeout(timer); channel.port1.close(); reject(error); }
    });
  }
  async check() {
    if (!this.supported()) return this.report({ state: "NOT_SUPPORTED", ready: false, message: "Offline access requires service-worker and cache support in a secure browser context." });
    try {
      this.registration = await navigator.serviceWorker.getRegistration();
      const controller = navigator.serviceWorker.controller;
      if (!this.registration?.active || !controller || controller.state !== "activated") return this.report({ state: "NOT_INSTALLED", ready: false, version: null, updatedAt: null, message: "No active service worker controls this page. Prepare offline access." });
      const result = await this.message(controller, "CHECK_CACHE");
      if (result.cacheId !== CACHE_ID || result.version !== APP_VERSION) return this.report({ state: "ERROR", ready: false, message: "The page and active cache versions differ. Install the available update." });
      return this.report({ ...result, state: this.registration.waiting ? "UPDATE_AVAILABLE" : result.state });
    } catch (error) { return this.report({ state: this.registration?.waiting ? "UPDATE_AVAILABLE" : "ERROR", ready: false, message: this.registration?.waiting ? "A new app version is ready to install." : error.message }); }
  }
  async prepare() {
    if (!this.supported()) return this.check();
    this.report({ state: "CACHING", ready: false, message: "Preparing and verifying application files…" });
    try {
      this.registration = await navigator.serviceWorker.register("./service-worker.js", { updateViaCache: "none" });
      this.registration.addEventListener("updatefound", () => {
        this.registration.installing?.addEventListener("statechange", () => { if (this.registration.waiting) this.check(); });
      });
      await Promise.race([navigator.serviceWorker.ready, new Promise((_, reject) => setTimeout(() => reject(new Error("Offline installation did not complete. Check your connection.")), 15000))]);
      if (!navigator.serviceWorker.controller) {
        await new Promise((resolve, reject) => {
          const timer = setTimeout(() => { navigator.serviceWorker.removeEventListener("controllerchange", changed); reject(new Error("The worker is installed but does not control this page. Reload and check again.")); }, 5000);
          const changed = () => { clearTimeout(timer); navigator.serviceWorker.removeEventListener("controllerchange", changed); resolve(); };
          navigator.serviceWorker.addEventListener("controllerchange", changed);
          if (navigator.serviceWorker.controller) changed();
        });
      }
      const current = await this.message(navigator.serviceWorker.controller, "CHECK_CACHE");
      if (!current.ready) {
        const prepared = await this.message(navigator.serviceWorker.controller, "PREPARE_CACHE");
        if (!prepared.ready) return this.report(prepared);
      }
      await this.registration.update().catch(() => {});
      return this.check();
    } catch (error) {
      if (this.registration?.waiting) return this.report({ state: "UPDATE_AVAILABLE", ready: false, message: "A new app version is ready to install." });
      return this.report({ state: "ERROR", ready: false, message: error.message });
    }
  }
  async applyUpdate() {
    this.registration ||= await navigator.serviceWorker.getRegistration();
    if (!this.registration?.waiting) return this.check();
    navigator.serviceWorker.addEventListener("controllerchange", () => location.reload(), { once: true });
    this.registration.waiting.postMessage({ type: "SKIP_WAITING" });
  }
}
