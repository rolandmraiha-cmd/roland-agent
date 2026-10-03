"use strict";
// Web chat page. All text is inserted with textContent, never as HTML.

const $ = (id) => document.getElementById(id);
let currentChat = null;
let sending = false;
let csrf = "";

async function api(path, options = {}) {
  const headers = { "X-CSRF-Token": csrf };
  if (options.body) headers["Content-Type"] = "application/json";
  const res = await fetch(path, { credentials: "same-origin", ...options, headers });
  if (res.status === 401) { location.href = "/login"; throw new Error("logged out"); }
  if (!res.ok) {
    let msg = res.statusText;
    try { const j = await res.json(); msg = j.detail || j.error || msg; } catch (_) {}
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return res;
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short" });
}

// ---------- sidebar ----------
function openSidebar(open) {
  $("sidebar").classList.toggle("open", open);
  $("scrim").hidden = !open;
}
$("menu").onclick = () => openSidebar(!$("sidebar").classList.contains("open"));
$("scrim").onclick = () => openSidebar(false);

async function loadStatus() {
  try {
    const s = await (await api("/api/status")).json();
    csrf = s.csrf;
    document.title = s.name;
    $("status").textContent = `${s.model} · ${s.calls_left}/${s.daily_limit} calls left today`;
  } catch (_) {}
}

async function loadChats() {
  const chats = await (await api("/api/chats")).json();
  const list = $("chat-list");
  list.replaceChildren();
  for (const c of chats) {
    const row = el("div", "chat-row" + (c.id === currentChat ? " active" : ""));
    const open = el("button", "chat-open", c.title);
    open.onclick = () => { openChat(c.id); openSidebar(false); };
    const del = el("button", "chat-del", "×");
    del.setAttribute("aria-label", "Delete chat");
    del.onclick = async () => {
      if (!confirm(`Delete "${c.title}"?`)) return;
      try { await api(`/api/chats/${c.id}`, { method: "DELETE" }); } catch (e) { alert(e.message); return; }
      if (c.id === currentChat) { currentChat = null; $("messages").replaceChildren(); $("title").textContent = "Chat"; }
      loadChats();
    };
    row.append(open, del);
    list.append(row);
  }
  return chats;
}

// ---------- chat ----------
function addMessage(role, text) {
  const m = el("div", `msg ${role}`);
  m.append(el("div", "bubble", text));
  $("messages").append(m);
  scrollDown();
  return m;
}

function scrollDown() {
  const box = $("messages");
  box.scrollTop = box.scrollHeight;
}

async function openChat(id) {
  currentChat = id;
  showView("chat");
  const data = await (await api(`/api/chats/${id}/messages`)).json();
  const box = $("messages");
  box.replaceChildren();
  for (const m of data.messages) addMessage(m.role, m.content);
  if (data.busy) addMessage("note", "The agent is still answering here… reopen the chat in a moment.");
  const chats = await loadChats();
  const c = chats.find((x) => x.id === id);
  $("title").textContent = c ? c.title : "Chat";
  $("input").focus();
}

async function newChat() {
  const { id } = await (await api("/api/chats", { method: "POST" })).json();
  await openChat(id);
  openSidebar(false);
}
$("new-chat").onclick = newChat;

async function send(text) {
  if (sending) return;
  if (currentChat === null) {
    const { id } = await (await api("/api/chats", { method: "POST" })).json();
    currentChat = id;
  }
  sending = true;
  $("send").disabled = true;
  addMessage("user", text);
  const reply = addMessage("assistant", "");
  const bubble = reply.querySelector(".bubble");
  reply.classList.add("typing");
  try {
    const res = await api(`/api/chats/${currentChat}/send`, { method: "POST", body: JSON.stringify({ text }) });
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, i);
        buf = buf.slice(i + 2);
        if (!chunk.startsWith("data: ")) continue;
        const ev = JSON.parse(chunk.slice(6));
        if (ev.type === "text") bubble.textContent += ev.text;
        else if (ev.type === "tool") reply.before(el("div", "msg tool", `⚙ ${ev.text}`));
        else if (ev.type === "error") reply.after(el("div", "msg error", ev.message));
        scrollDown();
      }
    }
  } catch (e) {
    reply.after(el("div", "msg error", e.message));
  } finally {
    reply.classList.remove("typing");
    if (!bubble.textContent) reply.remove();
    sending = false;
    $("send").disabled = false;
    loadChats().then((chats) => {
      const c = chats.find((x) => x.id === currentChat);
      if (c) $("title").textContent = c.title;
    });
    loadStatus();
  }
}

const input = $("input");
function autosize() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 200) + "px"; }
input.addEventListener("input", autosize);
input.addEventListener("keydown", (e) => {
  // Enter sends on a computer; on a phone the keyboard's Enter makes a new line.
  if (e.key === "Enter" && !e.shiftKey && !matchMedia("(pointer: coarse)").matches) {
    e.preventDefault();
    $("composer").requestSubmit();
  }
});
$("composer").onsubmit = (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text || sending) return;
  input.value = "";
  autosize();
  send(text);
};

// ---------- jobs ----------
function showView(name) {
  $("chat-view").hidden = name !== "chat";
  $("jobs-view").hidden = name !== "jobs";
  $("tab-chat").classList.toggle("active", name === "chat");
  $("tab-jobs").classList.toggle("active", name === "jobs");
  if (name === "jobs") loadJobs();
}
$("tab-chat").onclick = () => showView("chat");
$("tab-jobs").onclick = () => showView("jobs");

async function loadJobs() {
  const data = await (await api("/api/jobs")).json();
  $("tz").textContent = data.timezone;
  const list = $("job-list");
  list.replaceChildren();
  if (!data.jobs.length) list.append(el("p", "hint", "No jobs yet."));
  for (const j of data.jobs) {
    const card = el("div", "card job" + (j.enabled ? "" : " paused"));
    const head = el("div", "job-head");
    head.append(el("strong", "", j.name), el("code", "", j.cron));
    card.append(head, el("p", "job-prompt", j.prompt));
    card.append(el("p", "hint", j.enabled ? `Next run: ${fmtTime(j.next_run)}` : "Paused"));
    const actions = el("div", "job-actions");
    const runBtn = el("button", "ghost", "Run now");
    runBtn.onclick = async () => { await api(`/api/jobs/${j.id}/run`, { method: "POST" }); runBtn.textContent = "Running…"; setTimeout(loadJobs, 3000); };
    const toggle = el("button", "ghost", j.enabled ? "Pause" : "Resume");
    toggle.onclick = async () => { await api(`/api/jobs/${j.id}/toggle`, { method: "POST" }); loadJobs(); };
    const del = el("button", "ghost danger", "Delete");
    del.onclick = async () => { if (confirm(`Delete job "${j.name}"?`)) { await api(`/api/jobs/${j.id}`, { method: "DELETE" }); loadJobs(); } };
    actions.append(runBtn, toggle, del);
    card.append(actions);
    list.append(card);
  }
  const runs = $("run-list");
  runs.replaceChildren();
  if (!data.runs.length) runs.append(el("p", "hint", "Nothing has run yet."));
  for (const r of data.runs) {
    const d = el("details", "card run");
    const state = r.finished == null ? "running…" : r.ok ? "done" : "failed";
    const s = el("summary");
    s.append(el("span", `badge ${state === "done" ? "ok" : state === "failed" ? "bad" : ""}`, state),
             el("span", "", ` ${r.job_name} · ${fmtTime(r.started)}`));
    d.append(s, el("pre", "run-output", r.output || ""));
    runs.append(d);
  }
}

$("job-form").onsubmit = async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  $("job-error").textContent = "";
  try {
    await api("/api/jobs", { method: "POST", body: JSON.stringify(Object.fromEntries(f)) });
    e.target.reset();
    loadJobs();
  } catch (err) { $("job-error").textContent = err.message; }
};

$("logout").onclick = async () => {
  try { await api("/logout", { method: "POST" }); } catch (_) {}
  location.href = "/login";
};

// ---------- start ----------
(async () => {
  await loadStatus();
  const chats = await loadChats();
  if (chats.length) openChat(chats[0].id);
})();
