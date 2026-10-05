"use strict";
// Web chat page. All text is inserted with textContent, never as HTML.

const $ = (id) => document.getElementById(id);
let currentChat = null;
let sending = false;
let csrf = "";
let chatLoad = 0;

function setSending(value) {
  sending = value;
  $("send").disabled = value;
  $("new-chat").disabled = value;
}

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
      if (sending) return;
      if (!confirm(`Delete "${c.title}"?`)) return;
      try { await api(`/api/chats/${c.id}`, { method: "DELETE" }); } catch (e) { alert(e.message); return; }
      if (c.id === currentChat) { ++chatLoad; currentChat = null; $("messages").replaceChildren(); $("title").textContent = "Chat"; }
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
  if (sending) return;
  const load = ++chatLoad;
  currentChat = id;
  showView("chat");
  // Do not leave the previous conversation visible while this history loads.
  $("messages").replaceChildren();
  $("title").textContent = "Chat";
  let data;
  try {
    data = await (await api(`/api/chats/${id}/messages`)).json();
  } catch (e) {
    // Don't show an empty chat as if it had no messages.
    if (load === chatLoad && currentChat === id && !sending) addMessage("error", `Couldn't load this chat: ${e.message}`);
    return;
  }
  if (load !== chatLoad || sending) return;
  const box = $("messages");
  box.replaceChildren();
  for (const m of data.messages) addMessage(m.role, m.content);
  if (data.busy) addMessage("note", "The agent is still answering here… reopen the chat in a moment.");
  const chats = await loadChats();
  if (load !== chatLoad || sending) return;
  const c = chats.find((x) => x.id === id);
  $("title").textContent = c ? c.title : "Chat";
  $("input").focus();
}

async function newChat() {
  if (sending) return;
  setSending(true);
  ++chatLoad;
  let id;
  try {
    ({ id } = await (await api("/api/chats", { method: "POST" })).json());
  } catch (e) {
    addMessage("error", e.message);
    return;
  } finally {
    setSending(false);
  }
  try {
    await openChat(id);
    openSidebar(false);
  } catch (e) {
    addMessage("error", e.message);
  }
}
$("new-chat").onclick = newChat;

async function send(text) {
  if (sending) return;
  // Reserve the composer before the first await, including creation of a new chat.
  setSending(true);
  ++chatLoad; // An older history request must not replace this live conversation.
  let reply = null;
  try {
    if (currentChat === null) {
      const { id } = await (await api("/api/chats", { method: "POST" })).json();
      currentChat = id;
    }
    addMessage("user", text);
    reply = addMessage("assistant", "");
    const bubble = reply.querySelector(".bubble");
    reply.classList.add("typing");
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
    if (reply) reply.after(el("div", "msg error", e.message));
    else {
      addMessage("error", e.message);
      // Creation failed before a message was sent. Preserve the draft for retry.
      if (!input.value) { input.value = text; autosize(); }
    }
  } finally {
    if (reply) {
      reply.classList.remove("typing");
      if (!reply.querySelector(".bubble").textContent) reply.remove();
    }
    setSending(false);
    loadChats().then((chats) => {
      const c = chats.find((x) => x.id === currentChat);
      if (c) $("title").textContent = c.title;
    }).catch((e) => addMessage("error", e.message));
    loadStatus();
  }
}

const input = $("input");
function autosize() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 200) + "px"; }
input.addEventListener("input", autosize);
input.addEventListener("keydown", (e) => {
  // Enter sends on a computer; Ctrl/Cmd+Enter also sends with a phone/tablet keyboard.
  // Never submit the Enter used to confirm an IME composition.
  // Safari can report that Enter as a normal one, but with keyCode 229.
  if (e.key === "Enter" && !e.isComposing && e.keyCode !== 229 && !e.shiftKey &&
      (e.ctrlKey || e.metaKey || !matchMedia("(pointer: coarse)").matches)) {
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
    const waiting = !j.approved;
    const card = el("div", "card job" + (waiting ? " waiting" : j.enabled ? "" : " paused"));
    const head = el("div", "job-head");
    head.append(el("strong", "", j.name), el("code", "", j.cron));
    card.append(head, el("p", "job-prompt", j.prompt));
    const status = waiting
      ? (j.origin === "agent" ? "The agent made this job. " : "Where this job came from wasn't recorded (it was made before v1 tracked that), so the agent may have made it. ")
        + "Waiting for your OK: read what it does, then approve or delete it."
      : j.running ? "Running now…"
      : j.enabled ? `Next run: ${fmtTime(j.next_run)}` : "Paused";
    card.append(el("p", waiting ? "hint warn" : "hint", status));
    const actions = el("div", "job-actions");
    const msg = el("span", "error");
    const act = (fn) => async () => { msg.textContent = ""; try { await fn(); } catch (e) { msg.textContent = e.message; } };
    if (waiting) {
      const ok = el("button", "primary", "Approve");
      ok.onclick = act(async () => { await api(`/api/jobs/${j.id}/approve`, { method: "POST" }); loadJobs(); });
      actions.append(ok);
    } else {
      const runBtn = el("button", "ghost", j.running ? "Running…" : "Run now");
      runBtn.disabled = j.running;
      runBtn.onclick = act(async () => {
        await api(`/api/jobs/${j.id}/run`, { method: "POST" });
        runBtn.textContent = "Running…"; runBtn.disabled = true; setTimeout(loadJobs, 3000);
      });
      const toggle = el("button", "ghost", j.enabled ? "Pause" : "Resume");
      toggle.onclick = act(async () => { await api(`/api/jobs/${j.id}/toggle`, { method: "POST" }); loadJobs(); });
      actions.append(runBtn, toggle);
    }
    const del = el("button", "ghost danger", "Delete");
    del.onclick = act(async () => { if (confirm(`Delete job "${j.name}"?`)) { await api(`/api/jobs/${j.id}`, { method: "DELETE" }); loadJobs(); } });
    actions.append(del, msg);
    card.append(actions);
    list.append(card);
  }
  const facts = $("fact-list");
  facts.replaceChildren();
  if (!data.facts.length) facts.append(el("p", "hint", "Nothing saved yet."));
  for (const f of data.facts) {
    const row = el("div", "card fact");
    const del = el("button", "ghost danger", "Delete");
    del.onclick = async () => {
      if (!confirm("Delete this saved fact?")) return;
      try { await api(`/api/facts/${f.id}`, { method: "DELETE" }); loadJobs(); } catch (e) { alert(e.message); }
    };
    row.append(el("span", "", f.text), del);
    facts.append(row);
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
