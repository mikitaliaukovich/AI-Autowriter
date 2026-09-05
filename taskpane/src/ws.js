/**
 * WebSocket link to the local service.
 *
 * The service and the task pane are served from the same origin, so this is always
 * `wss://` on the page's own host — no mixed-content exception needed.
 *
 * Reconnects with capped exponential backoff, because the expected failure mode is the
 * user restarting the service while the pane stays open, and the pane should come back
 * on its own rather than needing Word to reload the add-in.
 */

const MIN_DELAY = 500;
const MAX_DELAY = 8000;

export class Link {
  constructor(path = "/ws") {
    this.url = `${location.origin.replace(/^http/, "ws")}${path}`;
    this.socket = null;
    this.delay = MIN_DELAY;
    this.closed = false;
    /** @type {(message: object) => void} */
    this.onMessage = () => {};
    /** @type {(connected: boolean, detail: string) => void} */
    this.onStatus = () => {};
    this._timer = null;
  }

  get connected() {
    return this.socket?.readyState === WebSocket.OPEN;
  }

  connect() {
    this.closed = false;
    if (this.socket && this.socket.readyState <= WebSocket.OPEN) return;

    let socket;
    try {
      socket = new WebSocket(this.url);
    } catch (error) {
      this.onStatus(false, String(error));
      this._scheduleReconnect();
      return;
    }
    this.socket = socket;

    socket.addEventListener("open", () => {
      this.delay = MIN_DELAY;
      this.onStatus(true, "");
    });

    socket.addEventListener("message", (event) => {
      let message;
      try {
        message = JSON.parse(event.data);
      } catch {
        return;
      }
      if (message && typeof message === "object") this.onMessage(message);
    });

    socket.addEventListener("close", () => {
      this.socket = null;
      this.onStatus(false, "");
      this._scheduleReconnect();
    });

    socket.addEventListener("error", () => {
      // "close" always follows, which is where reconnection is handled.
    });
  }

  _scheduleReconnect() {
    if (this.closed || this._timer) return;
    this._timer = setTimeout(() => {
      this._timer = null;
      this.connect();
    }, this.delay);
    this.delay = Math.min(this.delay * 2, MAX_DELAY);
  }

  send(message) {
    if (!this.connected) return false;
    this.socket.send(JSON.stringify(message));
    return true;
  }

  close() {
    this.closed = true;
    if (this._timer) clearTimeout(this._timer);
    this._timer = null;
    this.socket?.close();
    this.socket = null;
  }
}
