// PenBridge iPad client: captures Apple Pencil input and streams it to the laptop.
(() => {
  "use strict";
  const HOVER = 0, DOWN = 1, MOVE = 2, UP = 3, LEAVE = 4;
  const $ = (id) => document.getElementById(id);
  const surface = $("surface"), area = $("area"), mirrorImg = $("mirror");
  const ink = $("ink"), sel = $("sel"), cursor = $("cursor");
  const inkCtx = ink.getContext("2d"), selCtx = sel.getContext("2d");

  let ws = null, config = null, retryMs = 500;
  let geom = { x: 0, y: 0, w: 1, h: 1 };
  let mirrorOn = false, eraserOn = false, regionMode = false, screenshotMode = false;
  let lastInk = null, fadeTimer = null, regionStart = null, pendingFrame = false;
  let inkColor = "#e8edf3", screenshotImage = null, screenshotAnnotations = null;
  let scalingMode = "letterbox";  // "letterbox" or "fill"

  // ------------------------------------------------------------------ pairing
  function readToken() {
    const m = location.hash.match(/t=([\w-]+)/);
    if (m) { try { localStorage.setItem("penbridge-token", m[1]); } catch (_) {} return m[1]; }
    try { return localStorage.getItem("penbridge-token"); } catch (_) { return null; }
  }
  let token = readToken();

  $("pairForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const v = $("pairInput").value.trim().replace(/^.*t=/, "");
    if (!v) return;
    token = v;
    try { localStorage.setItem("penbridge-token", v); } catch (_) {}
    $("pair").classList.remove("open");
    connect();
  });

  // ---------------------------------------------------------------- websocket
  function connect() {
    if (!token) { $("pair").classList.add("open"); return; }
    setStatus("Connecting…", false);
    ws = new WebSocket(`ws://${location.host}/ws`);
    ws.binaryType = "blob";
    ws.onopen = () => {
      retryMs = 500;
      ws.send(JSON.stringify({ t: "hello", token }));
      if (mirrorOn) send({ t: "mirror", on: true });
      if (eraserOn) send({ t: "eraser", on: true });
    };
    ws.onmessage = (e) => (typeof e.data === "string" ? onConfig(JSON.parse(e.data)) : showFrame(e.data));
    ws.onclose = (e) => {
      config = null;
      if (e.code === 4003) {
        setStatus("Wrong pairing code", true);
        try { localStorage.removeItem("penbridge-token"); } catch (_) {}
        token = null; $("pair").classList.add("open");
        return;
      }
      if (e.code === 4000) { setStatus("Another device took over", true); return; }
      setStatus("Disconnected. Retrying…", true);
      setTimeout(connect, retryMs);
      retryMs = Math.min(retryMs * 2, 5000);
    };
  }

  function send(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  }

  function onConfig(msg) {
    if (msg.t !== "config") return;
    const changed = !config || config.active !== msg.active || config.aspect !== msg.aspect;
    config = msg;
    renderMonitors();
    $("mirrorBtn").disabled = !msg.mirrorAvailable;
    const m = msg.monitors[msg.active];
    const warn = msg.mode === "windows-pen" ? "" : ` · ${msg.mode === "windows-mouse" ? "mouse mode (no pressure)" : msg.mode}`;
    setStatus(`${m.label} ${m.width}×${m.height}${msg.region ? " · area locked" : ""}${warn}`, false);
    if (changed) { layout(); clearInk(); }
  }

  function setStatus(text, bad) {
    $("status").innerHTML = `<b></b>`;
    $("status").firstChild.textContent = text;
    $("status").classList.toggle("bad", bad);
  }

  // ------------------------------------------------------------------- layout
  // The active area keeps the target's aspect ratio so handwriting isn't stretched.
  function layout() {
    const vw = window.innerWidth, vh = window.innerHeight;
    const top = 56, pad = 8;
    const aw = vw - pad * 2, ah = vh - top - pad;
    const aspect = config ? config.aspect : aw / ah;
    let w = aw, h = aw / aspect;
    if (h > ah) { h = ah; w = ah * aspect; }
    geom = { x: (vw - w) / 2, y: top + (ah - h) / 2, w, h };
    Object.assign(area.style, { left: `${geom.x}px`, top: `${geom.y}px`, width: `${w}px`, height: `${h}px` });
    const dpr = window.devicePixelRatio || 1;
    for (const c of [ink, sel]) { c.width = Math.round(w * dpr); c.height = Math.round(h * dpr); }
    inkCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
    selCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  window.addEventListener("resize", layout);

  function norm(e) {
    return [(e.clientX - geom.x) / geom.w, (e.clientY - geom.y) / geom.h];
  }

  // ---------------------------------------------------------------- keyboard & scroll
  function sendKey(...vks) {
    send({ t: "key", vks });
  }

  function scrollArea(dir) {
    const cx = geom.x + geom.w / 2, cy = geom.y + geom.h / 2;
    const nx = (cx - geom.x) / geom.w, ny = (cy - geom.y) / geom.h;
    send({ t: "scroll", x: nx, y: ny, direction: dir > 0 ? 1 : -1 });
  }

  surface.addEventListener("wheel", (e) => {
    e.preventDefault();
    scrollArea(e.deltaY);
  });

  // Windows virtual key codes
  const VK = { CTRL: 0x11, SHIFT: 0x10, ALT: 0x12, C: 0x43, V: 0x56, TAB: 0x09, MENU: 0x12 };

  // ---------------------------------------------------------------- pen input
  function sample(ev, e) {
    const [x, y] = norm(e);
    return [ev, round4(x), round4(y), round4(e.pressure || 0), Math.round(e.tiltX || 0), Math.round(e.tiltY || 0)];
  }
  const round4 = (v) => Math.round(v * 10000) / 10000;
  const isPen = (e) => e.pointerType === "pen";
  const coalesced = (e) => (e.getCoalescedEvents ? e.getCoalescedEvents() : null) || [e];

  surface.addEventListener("pointerdown", (e) => {
    if (!isPen(e)) return;
    e.preventDefault();
    try { surface.setPointerCapture(e.pointerId); } catch (_) {}
    if (regionMode) { regionStart = norm(e); return; }
    send({ t: "b", pts: [sample(DOWN, e)] });
    startInk(e);
  });

  surface.addEventListener("pointermove", (e) => {
    if (!isPen(e)) return;
    e.preventDefault();
    moveCursor(e);
    if (regionMode) { if (regionStart) drawSelection(e); return; }
    if (e.buttons & 1) {
      const evs = coalesced(e);
      send({ t: "b", pts: evs.map((c) => sample(MOVE, c)) });
      evs.forEach(drawInk);
    } else {
      send({ t: "b", pts: [sample(HOVER, e)] });  // Pencil hover (M2+ iPad Pro)
    }
  });

  const penUp = (e) => {
    if (!isPen(e)) return;
    e.preventDefault();
    if (regionMode) { finishRegion(e); return; }
    send({ t: "b", pts: [sample(UP, e)] });
    endInk();
  };
  surface.addEventListener("pointerup", penUp);
  surface.addEventListener("pointercancel", penUp);
  surface.addEventListener("pointerleave", (e) => {
    if (!isPen(e) || e.buttons) return;
    cursor.style.display = "none";
    send({ t: "b", pts: [sample(LEAVE, e)] });
  });

  // Stop Safari scrolling, zooming or showing the magnifier.
  document.addEventListener("touchmove", (e) => e.preventDefault(), { passive: false });
  document.addEventListener("gesturestart", (e) => e.preventDefault());
  document.addEventListener("contextmenu", (e) => e.preventDefault());

  function moveCursor(e) {
    Object.assign(cursor.style, { display: "block", left: `${e.clientX}px`, top: `${e.clientY}px` });
  }

  // ------------------------------------------------ local ink (instant feedback)
  function startInk(e) {
    clearTimeout(fadeTimer);
    lastInk = { x: e.clientX - geom.x, y: e.clientY - geom.y };
  }
  function drawInk(e) {
    if (!lastInk) return;
    const x = e.clientX - geom.x, y = e.clientY - geom.y;
    inkCtx.globalCompositeOperation = eraserOn ? "destination-out" : "source-over";
    inkCtx.strokeStyle = eraserOn ? "#e8edf3" : inkColor;
    inkCtx.lineCap = "round";
    inkCtx.lineWidth = eraserOn ? 18 : 0.8 + (e.pressure || 0.3) * 3;
    inkCtx.beginPath();
    inkCtx.moveTo(lastInk.x, lastInk.y);
    inkCtx.lineTo(x, y);
    inkCtx.stroke();
    lastInk = { x, y };
  }
  function endInk() {
    lastInk = null;
    // With the screen mirrored, the real ink shows up in the image, so drop the preview.
    if (mirrorOn) fadeTimer = setTimeout(clearInk, 700);
  }
  function clearInk() { inkCtx.clearRect(0, 0, ink.width, ink.height); }

  // ------------------------------------------------------------- region select
  function drawSelection(e) {
    const [x, y] = norm(e);
    selCtx.clearRect(0, 0, sel.width, sel.height);
    selCtx.strokeStyle = "#3b82f6"; selCtx.lineWidth = 2; selCtx.setLineDash([8, 6]);
    selCtx.fillStyle = "rgba(59,130,246,.15)";
    const [sx, sy] = regionStart;
    const r = [sx * geom.w, sy * geom.h, (x - sx) * geom.w, (y - sy) * geom.h];
    selCtx.fillRect(...r); selCtx.strokeRect(...r);
  }
  function finishRegion(e) {
    if (!regionStart) return;
    const [x, y] = norm(e);
    send({ t: "region", rect: [regionStart[0], regionStart[1], x, y] });
    regionStart = null;
    setRegionMode(false);
  }
  function setRegionMode(on) {
    regionMode = on;
    selCtx.clearRect(0, 0, sel.width, sel.height);
    $("drawRegion").classList.toggle("on", on);
    $("hint").textContent = on ? "Drag a box with the Pencil around the part of the screen you want to write on." : "";
  }

  // -------------------------------------------------------------------- mirror
  function showFrame(blob) {
    if (!mirrorOn || pendingFrame) return;  // drop frames rather than queue lag
    pendingFrame = true;
    const url = URL.createObjectURL(blob);
    mirrorImg.onload = mirrorImg.onerror = () => { URL.revokeObjectURL(url); pendingFrame = false; };
    mirrorImg.src = url;
    mirrorImg.style.display = "block";
  }
  function setMirror(on) {
    mirrorOn = on;
    $("mirrorBtn").classList.toggle("on", on);
    if (!on) { mirrorImg.style.display = "none"; mirrorImg.removeAttribute("src"); }
    else clearInk();
    send({ t: "mirror", on });
  }

  // ------------------------------------------------------------------- toolbar
  function renderMonitors() {
    const row = $("monitors");
    row.replaceChildren(...config.monitors.map((m) => {
      const b = document.createElement("button");
      b.innerHTML = `<div style="font-weight:600">${m.label}</div><div style="font-size:10px;opacity:0.7">${m.width}×${m.height}</div>`;
      b.style.minHeight = "50px";
      b.style.padding = "4px 8px";
      b.classList.toggle("on", m.index === config.active);
      b.onclick = () => send({ t: "monitor", index: m.index });
      return b;
    }));
    $("full").classList.toggle("on", !config.region);
  }

  $("gear").onclick = () => $("panel").classList.toggle("open");
  $("full").onclick = () => { setRegionMode(false); send({ t: "region", rect: null }); };
  $("fitWin").onclick = () => {
    setRegionMode(false);
    send({ t: "fitWindow" });
    $("hint").textContent = "Locked to the window that is active on the laptop.";
  };
  $("drawRegion").onclick = () => {
    if (regionMode) { setRegionMode(false); return; }
    send({ t: "region", rect: null });
    if (!mirrorOn && config && config.mirrorAvailable) setMirror(true);
    setRegionMode(true);
  };
  $("mirrorBtn").onclick = () => setMirror(!mirrorOn);
  $("eraser").onclick = () => {
    eraserOn = !eraserOn;
    $("eraser").classList.toggle("on", eraserOn);
    send({ t: "eraser", on: eraserOn });
  };
  $("clear").onclick = clearInk;
  $("colorPicker").onchange = (e) => {
    inkColor = e.target.value;
  };
  $("refreshBtn").onclick = () => location.reload();
  $("copyBtn").onclick = () => sendKey(VK.CTRL, VK.C);
  $("pasteBtn").onclick = () => sendKey(VK.CTRL, VK.V);
  $("tabBtn").onclick = () => sendKey(VK.ALT, VK.TAB);
  $("scrollUpBtn").onclick = () => scrollArea(120);
  $("scrollDownBtn").onclick = () => scrollArea(-120);

  // Keep the iPad awake while writing (Safari 16.4+).
  async function keepAwake() {
    try { if ("wakeLock" in navigator) await navigator.wakeLock.request("screen"); } catch (_) {}
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden) keepAwake(); });

  layout();
  keepAwake();
  connect();
})();
