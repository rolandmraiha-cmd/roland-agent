"use strict";
// The screen page (§6.6): Roland watches or controls the agent's browser through noVNC.
// The VNC password arrives with the screen session and is handed straight to noVNC; it is
// never put in the page, the address or storage. Nothing typed here is logged, stored or
// sent to the agent: keys go over the screen connection to the browser and nowhere else.

const $ = (id) => document.getElementById(id);
const query = new URLSearchParams(location.search);
const signinId = query.get("signin") || null;
const HEARTBEAT_MS = 60000;
// X11 key codes for keys that put no text into the typing box.
const KEYS = {
  Enter: 0xff0d, Tab: 0xff09, Escape: 0xff1b, Backspace: 0xff08, Delete: 0xffff,
  ArrowLeft: 0xff51, ArrowUp: 0xff52, ArrowRight: 0xff53, ArrowDown: 0xff54,
};

let csrf = "";
let mode = query.get("mode") === "control" ? "control" : "watch";
let session = null;
let rfb = null;
let heartbeat = null;
let signinOpen = !!signinId;
let ended = false;
let typed = "";
let zoomed = false;

async function post(path, body, keepalive = false) {
  const res = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    keepalive,
    headers: { "X-CSRF-Token": csrf, "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (res.status === 401) { location.href = "/login"; throw new Error("logged out"); }
  if (!res.ok) {
    let message = res.statusText;
    try { const j = await res.json(); message = j.detail || j.error || message; } catch (_) {}
    throw new Error(typeof message === "string" ? message : JSON.stringify(message));
  }
  return res.json();
}

function say(text) { $("screen-status").textContent = text; }

function fail(text) {
  $("screen-error").textContent = text;
  $("screen-error").hidden = !text;
}

function renderButtons() {
  const live = !!session && !ended;
  const control = live && mode === "control";
  $("screen-mode").textContent = !live ? "Browser screen" : control ? "You have the browser" : "Watching the agent";
  $("screen-done").hidden = !(signinOpen && !ended);
  $("screen-keyboard").hidden = !control;
  $("screen-handback").hidden = !control;
  $("screen-watch").hidden = !control;
  $("screen-control").hidden = !(live && mode === "watch");
  $("screen-zoom").hidden = !live;
  $("screen-zoom").textContent = zoomed ? "Fit screen" : "Zoom in";
}

// The browser's screen is far wider than a phone. Fitted, the whole page shows but small;
// zoomed, it is full size and dragging moves around it (a tap still clicks).
function applyZoom() {
  if (!rfb) return;
  rfb.scaleViewport = !zoomed;
  rfb.clipViewport = zoomed;
  rfb.dragViewport = zoomed;
}

function disconnect() {
  if (heartbeat !== null) clearInterval(heartbeat);
  heartbeat = null;
  const old = rfb;
  rfb = null;
  if (old) { try { old.disconnect(); } catch (_) {} }
  $("screen").replaceChildren();
  typed = "";
  $("screen-typing").value = "";
}

async function release(keepalive = false) {
  const old = session;
  session = null;
  disconnect();
  if (old) { try { await post("/api/screen/release", { id: old.id }, keepalive); } catch (_) {} }
}

function over(text) {
  session = null;
  disconnect();
  say(text);
  renderButtons();
}

async function beat() {
  if (!session) return;
  try {
    await post("/api/screen/heartbeat", { id: session.id });
  } catch (e) {
    // 410: the session ran out or another one replaced it. A network blip just tries again.
    if (e.message === "that screen session is over") over("This screen session is over.");
  }
}

async function start(nextMode) {
  if (ended) return;
  fail("");
  await release();
  mode = nextMode;
  say("Connecting…");
  renderButtons();
  let opened;
  try {
    opened = await post("/api/screen/session", {
      mode, signin_id: mode === "control" && signinOpen ? signinId : undefined,
    });
  } catch (e) {
    if (/no longer waiting/.test(e.message)) signinOpen = false;
    fail(e.message);
    say("Not connected.");
    renderButtons();
    return;
  }
  const password = opened.vnc_password;
  session = { id: opened.id };
  try {
    // noVNC is served only while a screen session exists, so it is fetched after one starts.
    const { default: RFB } = await import("/screen/novnc/core/rfb.js");
    const scheme = location.protocol === "https:" ? "wss://" : "ws://";
    const current = new RFB($("screen"), scheme + location.host + opened.ws_path, { credentials: { password } });
    rfb = current;
    current.viewOnly = mode !== "control";
    current.focusOnClick = true;
    applyZoom();
    current.addEventListener("connect", () => {
      if (rfb === current) say(mode === "control" ? "Connected. The agent is paused." : "Connected. The agent keeps working.");
    });
    current.addEventListener("disconnect", () => {
      if (rfb !== current) return;
      // Give the browser back now, not when the idle timeout notices.
      const old = session;
      over("The screen was disconnected.");
      if (old) post("/api/screen/release", { id: old.id }).catch(() => {});
    });
    current.addEventListener("securityfailure", () => { if (rfb === current) fail("The screen refused the connection."); });
    current.addEventListener("credentialsrequired", () => { if (rfb === current) fail("The screen asked for a password it was not given."); });
  } catch (e) {
    await release();
    fail("The screen could not be opened.");
    say("Not connected.");
  }
  if (session) heartbeat = setInterval(beat, HEARTBEAT_MS);
  renderButtons();
}

function finish(text) {
  ended = true;
  over(text);
}

$("screen-done").onclick = async () => {
  $("screen-done").disabled = true;
  try {
    await post(`/api/signin/${encodeURIComponent(signinId)}/done`);
    signinOpen = false;
    finish("Done. The agent has the browser again; you can close this page.");
  } catch (e) {
    if (/no longer waiting/.test(e.message)) signinOpen = false;
    fail(e.message);
    renderButtons();
  } finally {
    $("screen-done").disabled = false;
  }
};

$("screen-handback").onclick = async () => {
  await release();
  say(signinOpen ? "Handed back. The sign-in is still waiting for I'm done or Cancel." : "The agent has the browser again.");
  renderButtons();
};
$("screen-zoom").onclick = () => { zoomed = !zoomed; applyZoom(); renderButtons(); };
$("screen-watch").onclick = () => start("watch");
$("screen-control").onclick = () => start("control");
$("screen-close").onclick = async () => {
  ended = true;
  await release();
  window.close();
  location.href = "/";  // a tab the page didn't open itself can't be closed by it
};

// Leaving the page gives the browser back at once instead of after the idle timeout.
window.addEventListener("pagehide", () => {
  if (!session) return;
  const old = session;
  session = null;
  try { post("/api/screen/release", { id: old.id }, true).catch(() => {}); } catch (_) {}
});

// ---------- typing from a phone ----------
// A phone only shows its keyboard for a text box, so this box takes the typing and each
// change is passed on as key presses. The box is emptied as it goes and is never read back.
function keysym(char) {
  const code = char.codePointAt(0);
  if (code === 0x0a || code === 0x0d) return KEYS.Enter;
  if (code === 0x09) return KEYS.Tab;
  if ((code >= 0x20 && code <= 0x7e) || (code >= 0xa0 && code <= 0xff)) return code;
  return 0x01000000 + code;
}

function press(sym) {
  if (rfb && mode === "control") rfb.sendKey(sym, null);
}

const typing = $("screen-typing");
$("screen-keyboard").onclick = () => typing.focus();
typing.addEventListener("input", () => {
  const now = typing.value;
  let same = 0;
  while (same < typed.length && same < now.length && typed[same] === now[same]) same += 1;
  // A phone keyboard may rewrite the word being typed: take back what changed, then retype it.
  for (let i = typed.length; i > same; i -= 1) press(KEYS.Backspace);
  for (const char of now.slice(same)) press(keysym(char));
  typed = now;
  if (typed.length > 200 || /\s$/.test(typed)) { typing.value = ""; typed = ""; }
});
typing.addEventListener("keydown", (event) => {
  if (event.isComposing || event.keyCode === 229) return;
  const sym = KEYS[event.key];
  if (sym === undefined) return;
  if (event.key === "Backspace" && typing.value !== "") return;  // the box handles it; "input" passes it on
  event.preventDefault();
  press(sym);
  typing.value = "";
  typed = "";
});
typing.addEventListener("blur", () => { typing.value = ""; typed = ""; });

// ---------- start ----------
(async () => {
  try {
    const status = await (await fetch("/api/status", { credentials: "same-origin" })).json();
    csrf = status.csrf || "";
  } catch (_) {
    fail("Could not reach the agent.");
    return;
  }
  await start(mode);
})();
