// Driving page: two multi-touch joysticks, keyboard control, speed limit, follow mode and connection status.
const $ = id => document.getElementById(id);

const store = {
  get(key, fallback) {
    try { const v = localStorage.getItem("picar." + key); return v === null ? fallback : JSON.parse(v); }
    catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem("picar." + key, JSON.stringify(value)); } catch { /* private mode */ }
  },
};

// ---------- joysticks ----------

// x is positive to the right and y positive downwards, both within the unit circle.
class Stick {
  constructor(el) {
    this.el = el;
    this.knob = el.querySelector(".knob");
    this.x = 0;
    this.y = 0;
    this.id = null;
    el.addEventListener("pointerdown", e => {
      if (this.id !== null) return;
      this.id = e.pointerId;
      el.setPointerCapture(e.pointerId);
      el.classList.add("active");
      this._move(e);
    });
    el.addEventListener("pointermove", e => { if (e.pointerId === this.id) this._move(e); });
    for (const type of ["pointerup", "pointercancel", "lostpointercapture"]) {
      el.addEventListener(type, e => { if (e.pointerId === this.id) this.release(); });
    }
    el.addEventListener("contextmenu", e => e.preventDefault());
  }

  _move(e) {
    const r = this.el.getBoundingClientRect();
    const reach = r.width / 2 - this.knob.offsetWidth / 2;
    let dx = (e.clientX - (r.left + r.width / 2)) / reach;
    let dy = (e.clientY - (r.top + r.height / 2)) / reach;
    const len = Math.hypot(dx, dy);
    if (len > 1) { dx /= len; dy /= len; }
    this._set(dx, dy);
  }

  _set(x, y) {
    this.x = x;
    this.y = y;
    const reach = this.el.clientWidth / 2 - this.knob.offsetWidth / 2;
    this.knob.style.transform = `translate(${x * reach}px, ${y * reach}px)`;
  }

  // Springs back to the middle. Further movement of the finger that held it is ignored.
  release() {
    this.id = null;
    this.el.classList.remove("active");
    this._set(0, 0);
  }
}

const drive = new Stick($("drive"));
const cam = new Stick($("cam"));

// ---------- keyboard ----------

const keys = new Set();
const KEYMAP = {
  drive: { up: ["KeyW", "ArrowUp"], down: ["KeyS", "ArrowDown"], left: ["KeyA", "ArrowLeft"], right: ["KeyD", "ArrowRight"] },
  cam: { up: ["KeyI"], down: ["KeyK"], left: ["KeyJ"], right: ["KeyL"] },
};
function keyAxis(map) {
  const down = names => names.some(n => keys.has(n));
  return { x: down(map.right) - down(map.left), y: down(map.down) - down(map.up) };
}
const settings = $("settings");
window.addEventListener("keydown", e => {
  if (settings.open || e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.code === "Space") { keys.clear(); link.setDirty({ follow: false }); e.preventDefault(); return; }
  if (e.code === "KeyC" && !e.repeat) { link.setDirty({ center_camera: true }); return; }
  if (Object.values(KEYMAP).some(m => Object.values(m).some(list => list.includes(e.code)))) {
    keys.add(e.code);
    e.preventDefault();
  }
});
window.addEventListener("keyup", e => keys.delete(e.code));

// ---------- settings ----------

let speedLimit = store.get("speed", 60);
const invert = { pan: store.get("inv_pan2", false), tilt: store.get("inv_tilt", false), steer: store.get("inv_steer", false) };

$("speed").value = speedLimit;
$("speed").addEventListener("input", e => {
  speedLimit = parseInt(e.target.value, 10);
  $("speed_out").textContent = speedLimit + "%";
  store.set("speed", speedLimit);
});
$("speed_out").textContent = speedLimit + "%";

// Pan is stored under a new key: an older build saved the opposite meaning under "inv_pan".
const storeKey = name => name === "pan" ? "inv_pan2" : "inv_" + name;
for (const name of ["pan", "tilt", "steer"]) {
  const box = $("inv_" + name);
  box.checked = invert[name];
  box.addEventListener("change", () => { invert[name] = box.checked; store.set(storeKey(name), box.checked); });
}

// Resolution is a setting of the car's camera, shared by every device, so it lives on the server.
const maxRes = $("max_res");
async function loadCamera() {
  try {
    const cam = await (await fetch("/api/camera")).json();
    maxRes.checked = cam.max;
    $("res_note").textContent = `(${cam.max_resolution}, default ${cam.default_resolution})`;
  } catch { /* offline: the checkbox stays as it was */ }
}
maxRes.addEventListener("change", async () => {
  try {
    await fetch("/api/camera", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ max: maxRes.checked }),
    });
    setTimeout(startVideo, 500);  // restart the stream at the new size
  } catch { maxRes.checked = !maxRes.checked; }
});
loadCamera();

$("open-settings").onclick = () => { keys.clear(); loadCamera(); loadObjects(); settings.showModal(); };
$("close-settings").onclick = () => settings.close();
settings.addEventListener("click", e => { if (e.target === settings) settings.close(); });  // backdrop

// Camera sliders show the real position; moving one sends an absolute position.
const sliders = { pan: $("pan"), tilt: $("tilt") };
const touched = { pan: 0, tilt: 0 };
for (const name of ["pan", "tilt"]) {
  sliders[name].addEventListener("input", () => {
    touched[name] = performance.now();
    $(name + "_out").textContent = sliders[name].value + "°";
    link.setDirty({ [name]: parseFloat(sliders[name].value) });
  });
}
$("center").onclick = () => link.setDirty({ center_camera: true });

$("stop").onclick = () => releaseAll();

// A page that is not in front must not keep driving.
const releaseAll = () => { drive.release(); cam.release(); keys.clear(); link.setDirty({ follow: false }); };
document.addEventListener("visibilitychange", () => { if (document.hidden) releaseAll(); });
window.addEventListener("blur", releaseAll);

// ---------- link ----------

// Small stick movements near the middle count as zero; the rest is rescaled to the full range.
const DEADZONE = 0.08;
const dz = v => Math.abs(v) < DEADZONE ? 0 : Math.sign(v) * (Math.abs(v) - DEADZONE) / (1 - DEADZONE);
const pick = (stick, fromKeys) => stick || fromKeys;  // a held stick wins over the keyboard

function build() {
  const kd = keyAxis(KEYMAP.drive), kc = keyAxis(KEYMAP.cam);
  const dx = pick(dz(drive.x), kd.x), dy = pick(dz(drive.y), kd.y);
  const cx = pick(dz(cam.x), kc.x), cy = pick(dz(cam.y), kc.y);
  return {
    steer: dx * (invert.steer ? -1 : 1),
    throttle: -dy * speedLimit / 100,
    // The camera pans the opposite way to the stick on this car, so "not inverted" flips the sign.
    pan_rate: cx * (invert.pan ? 1 : -1),
    tilt_rate: -cy * (invert.tilt ? -1 : 1),
  };
}

const video = $("video");
let videoTimer = 0;
// An <img> stream never reconnects by itself, so restart it after a reconnect or an error.
function startVideo() {
  $("novideo").hidden = false;
  video.src = "/video?" + Date.now();
  clearTimeout(videoTimer);
  videoTimer = setTimeout(() => { $("novideo").hidden = true; }, 4000);
}
video.onload = () => { $("novideo").hidden = true; };
video.onerror = () => { $("novideo").hidden = false; setTimeout(startVideo, 1000); };

const dot = $("dot"), conn = $("conn");
let status = "connecting";
function setStatus(s) {
  status = s;
  $("takeover").hidden = s !== "taken";
  if (s === "open") { startVideo(); showIp(); }
  if (s === "taken") { video.removeAttribute("src"); releaseAll(); }
  refreshIndicator();
}
async function showIp() {
  try {
    const st = await (await fetch("/api/status")).json();
    $("ip").textContent = st.ip || "";
  } catch { /* keep the last value */ }
}
function refreshIndicator() {
  const st = link.stats();
  let cls = "", text;
  if (status === "open") {
    const stale = st.sincePong > 3000;
    cls = stale || (st.avg !== null && st.avg > 150) ? "warn" : "ok";
    text = stale ? "no reply" : st.avg === null ? "connected" : `${Math.round(st.avg)} ms`;
  } else if (status === "taken") {
    text = "taken over";
  } else {
    text = status === "connecting" ? "connecting" : "disconnected";
  }
  dot.className = "dot " + cls;
  conn.textContent = text;
  if (settings.open && st.avg !== null) {
    $("stats").textContent = `round trip avg ${Math.round(st.avg)} ms, worst ${Math.round(st.worst)} ms; ` +
      `send-loop worst gap ${Math.round(st.worstGap || 0)} ms (10 s window)`;
  }
}

const link = new Link({
  build,
  onStatus: setStatus,
  onFollow: m => { followState = m; if (m.message) flash(m.message); renderFollow(); },
  onState: m => {
    const now = performance.now();
    for (const name of ["pan", "tilt"]) {
      const s = sliders[name];
      [s.min, s.max] = m.limits[name];
      if (now - touched[name] > 600) s.value = m[name];
      $(name + "_out").textContent = Math.round(m[name]) + "°";
    }
  },
});
$("retake").onclick = () => link.connect();

// ---------- follow mode ----------

// The car looks for what is inside the white corners (Learn), then drives after it (Follow). The server
// ends follow mode itself when the object is lost, a stick is touched or the connection is weak.
const el = (tag, className) => Object.assign(document.createElement(tag), className ? { className } : {});
const ENDED = {
  "manual control": "You took over", "stopped": "Stopped", "no input": "Lost the signal", "target lost": "Lost the object",
  "camera size changed": "Camera size changed", "tracker error": "Tracker error", "error": "Error",
};
const LOST = {
  drift: "Lost it: it looks different now", searching: "Looking for it...", ambiguous: "Two similar objects, not sure which",
  "track-only": "Lost it. Show it again and tap Learn", lost: "Lost it",
};
let followState = { learned: false, following: false };
let followNote = "", followNoteTimer = 0;
function flash(text) {
  followNote = text;
  clearTimeout(followNoteTimer);
  followNoteTimer = setTimeout(() => { followNote = ""; renderFollow(); }, 4000);
}
function followText(f) {
  if (followNote) return followNote;
  if (!f.learned) return "Hold an object in the corners";
  const pct = Math.round(f.confidence * 100) + "%";
  let text = f.state === "tracking" ? (f.following ? (f.drive === false ? "Following (camera only) " : "Following ") : "Tracking ") + pct : LOST[f.reason] || "Lost it";
  if (f.state === "tracking" && f.tier === "track-only") text += " (no colour: cannot be found again)";
  if (!f.following && f.ended && ENDED[f.ended]) text = ENDED[f.ended] + " · " + text;
  return text;
}
function renderFollow() {
  const f = followState;
  $("follow_status").textContent = followText(f);
  $("learn").hidden = f.learned && f.state !== "lost";  // a lost object can be learned again
  $("follow").hidden = !f.learned;
  $("forget").hidden = !f.learned || f.following;
  $("follow").textContent = f.following ? "Stop following" : "Follow";
  $("follow").classList.toggle("on", !!f.following);
  $("follow").disabled = !f.following && !(f.state === "tracking" && f.confidence >= 0.35);
  $("obj_save").disabled = !(f.learned && f.tier === "colour");
}
async function followCall(method, path, body) {
  try {
    const r = await fetch(path, { method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
    const data = await r.json();
    if (!r.ok) { flash(typeof data.detail === "string" ? data.detail : "That name does not work"); return null; }
    return data;
  } catch { flash("No connection"); return null; }
}
$("learn").onclick = async () => { const f = await followCall("POST", "/api/follow/learn"); if (f) { followState = f; renderFollow(); } };
$("forget").onclick = async () => { const f = await followCall("DELETE", "/api/follow"); if (f) { followState = f; renderFollow(); } };
$("follow").onclick = () => link.setDirty({ follow: !followState.following });

// Saved objects: the car looks for a loaded one and follows it once it is found.
async function loadObjects() {
  const names = await followCall("GET", "/api/objects");
  if (!names) return;
  const box = $("objects");
  box.replaceChildren();
  if (!names.length) { const none = el("small", "stats"); none.textContent = "none saved yet"; box.append(none); }
  for (const name of names) {
    const path = "/api/objects/" + encodeURIComponent(name);
    const row = el("div", "object"), picture = el("img"), label = el("span"), use = el("button"), del = el("button");
    picture.alt = "";
    picture.src = path + "/thumbnail";
    label.textContent = name;
    use.textContent = "Use";
    use.onclick = async () => { const f = await followCall("POST", path + "/load"); if (f) { followState = f; renderFollow(); settings.close(); } };
    del.textContent = "Delete";
    del.onclick = async () => { if (await followCall("DELETE", path)) loadObjects(); };
    row.append(picture, label, use, del);
    box.append(row);
  }
}
$("obj_save").onclick = async () => {
  const name = $("obj_name").value.trim();
  if (name && await followCall("POST", "/api/objects", { name })) { $("obj_name").value = ""; loadObjects(); }
};
renderFollow();
fetch("/api/follow").then(r => r.json()).then(f => { followState = f; renderFollow(); }).catch(() => {});
setInterval(refreshIndicator, 500);
