// WebSocket link to the car, shared by the driving page and the calibration page.
//
// build() is called every 30 ms and returns the continuous part of the command (steer, throttle,
// camera rates). The server stops the car if this stream stops, so it is sent even when idle.
// Absolute camera moves go through setDirty() and are sent once, so a stale page never
// overrides the camera. Follow mode starts and stops the same way ({follow: true or false}).
class Link {
  constructor({ build, onState, onStatus, onFollow }) {
    this.build = build;
    this.onState = onState || (() => {});
    this.onStatus = onStatus || (() => {});
    this.onFollow = onFollow || (() => {});
    this.ws = null;
    this.dirty = {};
    this.rtts = [];   // [time, ms]
    this.ticks = [];  // [time, gap ms]; a big gap means the phone itself stalled
    this.lastTick = 0;
    this.lastPing = 0;
    this.lastPong = 0;
    this.taken = false;
    setInterval(() => this._tick(), 30);
    setTimeout(() => this.connect(), 0);  // after the caller has stored the Link
  }

  get open() { return this.ws && this.ws.readyState === WebSocket.OPEN; }

  setDirty(fields) { Object.assign(this.dirty, fields); }

  connect() {
    this.taken = false;
    this.onStatus("connecting");
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${scheme}://${location.host}/ws`);
    this.ws = ws;
    ws.onopen = () => { this.lastPong = performance.now(); this.onStatus("open"); };
    ws.onclose = e => {
      if (this.ws !== ws) return;
      // Code 4000: another device connected. Do not fight it by reconnecting.
      if (e.code === 4000) { this.taken = true; this.onStatus("taken"); return; }
      this.onStatus("closed");
      setTimeout(() => { if (this.ws === ws) this.connect(); }, 1000);
    };
    ws.onmessage = e => {
      const m = JSON.parse(e.data);
      if (m.type === "pong") {
        const now = performance.now();
        this.lastPong = now;
        this.rtts.push([now, now - m.t]);
      } else if (m.type === "state") {
        this.onState(m);
      } else if (m.type === "follow") {
        this.onFollow(m);
      }
    };
  }

  _tick() {
    if (!this.open) return;
    const now = performance.now();
    if (this.lastTick) this.ticks.push([now, now - this.lastTick]);
    this.lastTick = now;
    const msg = { ...this.build(), ...this.dirty };
    this.dirty = {};
    if (now - this.lastPing > 1000) { msg.ping = now; this.lastPing = now; }
    this.ws.send(JSON.stringify(msg));
  }

  // Round trip and send-loop numbers over the last 10 seconds.
  stats() {
    const now = performance.now();
    const recent = list => {
      while (list.length && now - list[0][0] > 10000) list.shift();
      return list.map(s => s[1]);
    };
    const r = recent(this.rtts), t = recent(this.ticks);
    return {
      avg: r.length ? r.reduce((a, b) => a + b, 0) / r.length : null,
      worst: r.length ? Math.max(...r) : null,
      worstGap: t.length ? Math.max(...t) : null,
      sincePong: now - this.lastPong,
    };
  }
}
