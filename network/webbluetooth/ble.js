export const SERVICE_UUID = "7a1c0001-9e67-4a21-bde4-33c5eab07a01";
export const BOOTSTRAP_UUID = SERVICE_UUID.replace("0001-", "0002-");
export const COMMAND_UUID = SERVICE_UUID.replace("0001-", "0003-");
export const EVENT_UUID = SERVICE_UUID.replace("0001-", "0004-");
const encoder = new TextEncoder();
const decoder = new TextDecoder("utf-8", { fatal: true });
export const bytes = (text) => encoder.encode(text);
const encode = (value) => bytes(JSON.stringify(value));

export function frames(data, messageId) {
  if (!data.length || data.length > 32768) throw new Error("Message exceeds the transfer limit.");
  const output = [];
  for (let offset = 0, index = 0; offset < data.length; offset += 13, index++) {
    const frame = new Uint8Array(7 + Math.min(13, data.length - offset));
    const view = new DataView(frame.buffer);
    frame[0] = 0xA1;
    view.setUint16(1, messageId);
    view.setUint16(3, index);
    view.setUint16(5, data.length);
    frame.set(data.subarray(offset, offset + 13), 7);
    output.push(frame);
  }
  return output;
}

export class Reassembler {
  reset() { this.id = null; this.total = 0; this.index = 0; this.buffer = []; this.updated = 0; }
  constructor() { this.reset(); }
  feed(frame, now = Date.now()) {
    try {
      if (frame.length <= 7 || frame.length > 20) throw new Error("Invalid Bluetooth frame size.");
      const view = new DataView(frame.buffer, frame.byteOffset, frame.byteLength);
      const id = view.getUint16(1), index = view.getUint16(3), total = view.getUint16(5);
      if (frame[0] !== 0xA1 || !total || total > 32768) throw new Error("Invalid Bluetooth frame header.");
      if (this.id !== null && now - this.updated > 30000) this.reset();
      if (this.id === null) { if (index !== 0) throw new Error("Incomplete Bluetooth response. Reconnect and retry."); this.id = id; this.total = total; }
      if (id !== this.id || total !== this.total || index !== this.index || frame.length - 7 !== Math.min(13, total - this.buffer.length)) throw new Error("Bluetooth response is out of order or incomplete.");
      this.buffer.push(...frame.subarray(7)); this.index++; this.updated = now;
      if (this.buffer.length === total) { const result = new Uint8Array(this.buffer); this.reset(); return result; }
      return null;
    } catch (error) { this.reset(); throw error; }
  }
}

export class AquaBluetooth extends EventTarget {
  constructor() {
    super(); this.pending = new Map(); this.reassembly = new Reassembler(); this.messageId = 0;
    this.authorized = false; this.securityVerified = false; this.gattQueue = Promise.resolve();
    this.onDisconnect = () => { this.fail(new Error("Bluetooth disconnected. Reconnect and verify the Admin password.")); this.dispatchEvent(new Event("disconnected")); };
  }
  operation(work) {
    const result = this.gattQueue.then(work);
    this.gattQueue = result.catch(() => {});
    return result;
  }
  async bootstrapSession() {
    const view = await this.operation(() => this.bootstrap.readValue());
    const hello = JSON.parse(decoder.decode(new Uint8Array(view.buffer, view.byteOffset, view.byteLength)));
    if (hello.v !== 2 || hello.security !== "authenticated_ble_pairing_required" || !/^[0-9a-f]{32}$/i.test(hello.session)) throw new Error("This Pi requires the current secure provisioning service (protocol 2).");
    this.sid = hello.session; this.counter = 0; this.needsBootstrap = false;
    return hello;
  }
  async connect() {
    this.disconnect();
    this.device = await navigator.bluetooth.requestDevice({ filters: [{ services: [SERVICE_UUID] }] });
    this.device.addEventListener("gattserverdisconnected", this.onDisconnect);
    try {
      const server = await this.device.gatt.connect();
      const service = await server.getPrimaryService(SERVICE_UUID);
      this.command = await service.getCharacteristic(COMMAND_UUID);
      this.events = await service.getCharacteristic(EVENT_UUID);
      this.bootstrap = await service.getCharacteristic(BOOTSTRAP_UUID);
      this.hello = await this.bootstrapSession();
      // BlueZ enforces encrypt-authenticated-read here BEFORE the UI accepts any
      // Admin password. Just Works bonds cannot satisfy this permission.
      try { await this.events.readValue(); }
      catch { throw new Error("Authenticated Bluetooth pairing is required. Pair this phone using the Pi's physical/local pairing window. No Admin password was sent."); }
      this.securityVerified = true;
      this.startReader();
      await this.request("auth_state");
      return this.device;
    } catch (error) { this.disconnect(); throw error; }
  }
  startReader() {
    const device = this.device;
    this.reader = (async () => {
      try {
        while (this.device === device && device.gatt.connected) {
          const view = await this.operation(() => this.events.readValue());
          const packet = new Uint8Array(view.buffer, view.byteOffset, view.byteLength);
          if (packet.length) this.receive(packet);
          else await new Promise((done) => setTimeout(done, this.pending.size ? 150 : 1000));
        }
      } catch {
        if (this.device === device) { this.fail(new Error("Protected Bluetooth response unavailable. Reconnect and verify again.")); this.disconnect(); }
      }
    })();
  }
  async authorize(password) {
    if (!this.securityVerified) throw new Error("Authenticated, encrypted Bluetooth pairing is required before credential submission.");
    if (this.needsBootstrap) await this.bootstrapSession();
    const pending = this.request("admin_auth", { password });
    password = null;
    const result = await pending;
    if (!result.authorized) { const error = new Error("Unable to verify the Admin password."); error.retryAfter = result.retry_after_seconds || 0; throw error; }
    this.authorized = true;
    clearTimeout(this.expiry);
    this.expiry = setTimeout(() => this.disconnect(), result.expires_in * 1000);
    return result;
  }
  receive(frame) {
    const data = this.reassembly.feed(frame);
    if (!data) return;
    const message = JSON.parse(decoder.decode(data));
    if (message.session && message.session !== this.sid) throw new Error("Response session mismatch.");
    if (message.type === "authorization_expired" || message.error?.category === "AUTH_REQUIRED") {
      this.authorized = false; this.needsBootstrap = true;
      this.dispatchEvent(new CustomEvent("authorizationrequired", { detail: message.message || message.error?.message || "Admin verification required." }));
    }
    this.dispatchEvent(new CustomEvent("status", { detail: message }));
    const pending = this.pending.get(message.id);
    if (message.final && pending) {
      if (message.type === "error") { const error = new Error(message.error.message); error.retryAfter = message.retry_after_seconds || 0; pending.reject(error); }
      else pending.resolve(message.payload);
    }
  }
  async request(type, payload = {}) {
    if (!this.securityVerified) throw new Error("Authenticated Bluetooth link required.");
    if (!this.authorized && !["admin_auth", "auth_state"].includes(type)) throw new Error("Verify the Admin password first.");
    if (this.pending.size) throw new Error("Wait for the current Bluetooth request.");
    const id = crypto.randomUUID();
    const request = { id, type, payload, session: this.sid, counter: ++this.counter };
    const encoded = encode(request);
    let packets = frames(encoded, ++this.messageId % 65536);
    encoded.fill(0);
    if (Object.hasOwn(payload, "password")) payload.password = null;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.get(id)?.reject(new Error("Bluetooth request timed out. Reconnect and check status before retrying.")); this.disconnect(); }, 180000);
      const cleanup = () => { clearTimeout(timer); this.pending.delete(id); };
      this.pending.set(id, { resolve: (value) => { cleanup(); resolve(value); }, reject: (error) => { cleanup(); reject(error); } });
      (async () => {
        try { for (const packet of packets) { await this.operation(() => this.command.writeValueWithResponse(packet)); packet.fill(0); } }
        catch (error) { this.pending.get(id)?.reject(error); }
        finally { for (const packet of packets) packet.fill(0); packets = []; }
      })();
    });
  }
  fail(error) {
    for (const pending of [...this.pending.values()]) pending.reject(error);
    this.authorized = false; this.securityVerified = false; this.sid = null;
    this.reassembly.reset(); clearTimeout(this.expiry);
  }
  disconnect() {
    const wasConnected = Boolean(this.device?.gatt?.connected);
    this.fail(new Error("Bluetooth disconnected."));
    if (this.device) { this.device.removeEventListener("gattserverdisconnected", this.onDisconnect); this.device.gatt?.disconnect(); }
    this.events = null; this.command = null; this.bootstrap = null; this.device = null;
    this.gattQueue = Promise.resolve();
    if (wasConnected) this.dispatchEvent(new Event("disconnected"));
  }
}
