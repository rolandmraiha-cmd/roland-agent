"use strict";
// Web chat page. All text is inserted with textContent, never as HTML.

const $ = (id) => document.getElementById(id);
let currentChat = null;
let sending = false;
let csrf = "";
let chatLoad = 0;
let pendingApprovalCount = 0;
let composerLocked = false;
let activeView = "chat";

function setSending(value) {
  sending = value;
  $("send").disabled = value || composerLocked;
  $("new-chat").disabled = value;
  const stop = $("stop");
  if (stop) stop.hidden = !value;
}

function setComposerLocked(locked) {
  composerLocked = locked;
  const form = $("composer");
  if (form) {
    if (locked) form.classList.add("locked");
    else form.classList.remove("locked");
  }
  $("send").disabled = sending || locked;
  if ($("input")) $("input").disabled = locked;
}

function setApprovalBadge(n) {
  pendingApprovalCount = n;
  const badge = $("approval-badge");
  if (!badge) return;
  badge.textContent = String(n);
  badge.hidden = n <= 0;
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
    if (typeof s.pending_approvals === "number") setApprovalBadge(s.pending_approvals);
    if (activeView === "browser") await loadBrowserStatus();
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
  let hasPending = false;
  if (Array.isArray(data.pending_approvals)) {
    for (const a of data.pending_approvals) {
      if (a.status === "pending") {
        hasPending = true;
        $("messages").append(renderApprovalCard(a));
      }
    }
  }
  setComposerLocked(hasPending);
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
  let sawError = false;
  try {
    if (currentChat === null) {
      const { id } = await (await api("/api/chats", { method: "POST" })).json();
      currentChat = id;
    }
    addMessage("user", text);
    reply = addMessage("assistant", "");
    const bubble = reply.querySelector(".bubble");
    reply.classList.add("typing");
    const thinkStarted = Date.now();
    let gotText = false;
    const thinkTimer = setInterval(() => {
      if (gotText) return;
      const secs = Math.floor((Date.now() - thinkStarted) / 1000);
      bubble.textContent = `thinking… (local model, this can take a minute) ${secs}s`;
    }, 1000);
    bubble.textContent = "thinking… (local model, this can take a minute) 0s";
    const res = await api(`/api/chats/${currentChat}/send`, { method: "POST", body: JSON.stringify({ text }) });
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    const applyEvent = (ev) => {
      if (ev.type === "text") {
        if (!gotText) {
          gotText = true;
          bubble.textContent = "";
          clearInterval(thinkTimer);
          reply.classList.remove("typing"); // drop ◦ caret; avoids a flash on replace
        }
        bubble.textContent += ev.text;
      } else if (ev.type === "done") {
        // Authoritative final reply when non-empty. Required when the model never streamed
        // text deltas (common with JSON/grammar actions). Empty reply must not wipe streamed text.
        clearInterval(thinkTimer);
        reply.classList.remove("typing");
        if (typeof ev.reply === "string" && ev.reply) {
          gotText = true;
          bubble.textContent = ev.reply;
        } else if (!gotText) {
          bubble.textContent = "";
        }
      } else if (ev.type === "tool") {
        const badge = ev.decision && ev.decision !== "safe" ? ` [${ev.decision}]` : "";
        reply.before(el("div", "msg tool", `⚙ ${ev.text}${badge}`));
      } else if (ev.type === "approval_required") {
        setComposerLocked(true);
        const card = renderApprovalCard(ev.approval);
        reply.before(card);
        loadStatus();
      } else if (ev.type === "approval_resolved") {
        setComposerLocked(false);
        loadStatus();
      } else if (ev.type === "file") {
        reply.before(renderFileCard(ev));
      } else if (ev.type === "note") reply.before(el("div", "msg note", ev.text || ev.message || ""));
      else if (ev.type === "error") {
        sawError = true;
        clearInterval(thinkTimer);
        reply.classList.remove("typing");
        // Clear the thinking placeholder so the error isn't paired with a stuck spinner.
        if (!gotText) bubble.textContent = "";
        reply.after(el("div", "msg error", ev.message));
      }
      // "end" and SSE comments are ignored.
      scrollDown();
    };
    const consume = () => {
      // Accept both LF and CRLF event separators (proxies may normalize).
      buf = buf.replace(/\r\n/g, "\n");
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, i).trim();
        buf = buf.slice(i + 2);
        if (!chunk.startsWith("data: ")) continue;
        applyEvent(JSON.parse(chunk.slice(6)));
      }
    };
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (value) buf += decoder.decode(value, { stream: true });
        if (done) {
          buf += decoder.decode(); // flush any trailing multibyte sequence
          consume();
          break;
        }
        consume();
      }
    } finally { clearInterval(thinkTimer); }
  } catch (e) {
    if (reply) {
      sawError = true;
      reply.after(el("div", "msg error", e.message));
    } else {
      addMessage("error", e.message);
      // Creation failed before a message was sent. Preserve the draft for retry.
      if (!input.value) { input.value = text; autosize(); }
    }
  } finally {
    if (reply) {
      reply.classList.remove("typing");
      let final = reply.querySelector(".bubble").textContent;
      // Idle drop / partial SSE: server may still have saved THIS turn's reply.
      // Never recover after an error (would resurrect a prior assistant on chats with history).
      // Only accept an assistant message that follows the latest user message.
      if (!sawError && (!final || final.startsWith("thinking…")) && currentChat != null) {
        try {
          const data = await (await api(`/api/chats/${currentChat}/messages`)).json();
          const msgs = data.messages || [];
          let lastUser = -1;
          for (let i = msgs.length - 1; i >= 0; i--) {
            if (msgs[i].role === "user") { lastUser = i; break; }
          }
          const fresh = lastUser >= 0
            ? msgs.slice(lastUser + 1).filter((m) => m.role === "assistant").pop()
            : null;
          if (fresh && fresh.content) {
            reply.querySelector(".bubble").textContent = fresh.content;
            final = fresh.content;
          }
        } catch (_) {}
      }
      // Drop an empty bubble or a leftover thinking placeholder.
      if (!final || final.startsWith("thinking…")) reply.remove();
    }
    setSending(false);
    // Safety net: if approval_resolved was missed (stop/expire/cancel), do not leave the composer locked.
    setComposerLocked(false);
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
  if (!text || sending || composerLocked) return;
  input.value = "";
  autosize();
  send(text);
};


// ---------- approvals ----------
// Approvals that need confirming take two taps: the second between 1 and 5 seconds after the first.
const CONFIRM_MIN_GAP_MS = 1000;
const CONFIRM_WINDOW_MS = 5000;

function renderApprovalCard(approval, { compact } = {}) {
  const card = el("div", "card approval-card");
  card.setAttribute("data-approval-id", approval.id);
  card.append(el("div", "badge", approval.category || "other"));
  card.append(el("div", "summary", approval.summary || ""));
  if (approval.tainted) {
    card.append(el("p", "taint", "This run has read web pages, files or command output. Check this carefully."));
  }
  if (approval.model_reason) {
    card.append(el("p", "agent-says", "The agent says: " + approval.model_reason));
  }
  // Browser actions carry a picture of the page, taken when the agent asked. Only the
  // server's own preview route is ever used as the image address.
  const shot = approval.screenshot_url;
  if (typeof shot === "string" && shot.startsWith("/api/files/preview?path=screenshots/")) {
    const img = el("img", "shot");
    img.alt = "What the browser showed when the agent asked";
    img.loading = "lazy";
    img.src = shot;
    card.append(img);
  }
  const details = approval.details || {};
  const dl = el("dl", "");
  for (const [key, value] of Object.entries(details)) {
    dl.append(el("dt", "", key));
    dl.append(el("dd", "", typeof value === "string" ? value : JSON.stringify(value)));
  }
  card.append(dl);
  if (approval.args) {
    const exact = el("details", "exact");
    exact.append(el("summary", "", "Exact action"));
    exact.append(el("pre", "", JSON.stringify(approval.args, null, 2)));
    card.append(exact);
  }
  const expires = approval.expires ? new Date(approval.expires * 1000) : null;
  if (expires) card.append(el("p", "hint", "Expires: " + expires.toLocaleString()));
  if (approval.status && approval.status !== "pending") {
    card.append(el("p", "hint", "Status: " + approval.status));
    return card;
  }
  const actions = el("div", "actions");
  const note = el("input", "note");
  note.placeholder = "Reject note (optional)";
  const approveBtn = el("button", "primary", "Approve");
  const rejectBtn = el("button", "ghost danger", "Reject");
  let confirmArmed = false;
  let confirmArmedAt = 0;
  let confirmTimer = null;
  const disarm = () => {
    if (confirmTimer) clearTimeout(confirmTimer);
    confirmTimer = null;
    confirmArmed = false;
    approveBtn.textContent = "Approve";
  };
  approveBtn.onclick = async () => {
    if (approval.needs_confirm && !confirmArmed) {
      confirmArmed = true;
      confirmArmedAt = Date.now();
      approveBtn.textContent = "Tap again to confirm";
      confirmTimer = setTimeout(disarm, CONFIRM_WINDOW_MS);
      return;
    }
    // Both clicks of a double-click land within a few hundred milliseconds. The second tap
    // has to be a separate decision, so it only counts after a pause.
    if (approval.needs_confirm && Date.now() - confirmArmedAt < CONFIRM_MIN_GAP_MS) return;
    if (confirmTimer) clearTimeout(confirmTimer);
    approveBtn.disabled = true;
    rejectBtn.disabled = true;
    try {
      await api(`/api/approvals/${approval.id}/approve`, {
        method: "POST",
        body: JSON.stringify({ args_hash: approval.args_hash, confirm: !!approval.needs_confirm }),
      });
      approveBtn.textContent = "Approved";
      card.append(el("p", "hint", "Approved."));
      setComposerLocked(false);
      loadStatus();
      if ($("approvals-view") && !$("approvals-view").hidden) loadApprovals();
    } catch (e) {
      disarm();  // a failed approval starts over at the first tap
      approveBtn.disabled = false;
      rejectBtn.disabled = false;
      card.append(el("p", "error", e.message));
    }
  };
  rejectBtn.onclick = async () => {
    approveBtn.disabled = true;
    rejectBtn.disabled = true;
    try {
      await api(`/api/approvals/${approval.id}/reject`, {
        method: "POST",
        body: JSON.stringify({ note: note.value || undefined, args_hash: approval.args_hash }),
      });
      card.append(el("p", "hint", "Rejected."));
      setComposerLocked(false);
      loadStatus();
      if ($("approvals-view") && !$("approvals-view").hidden) loadApprovals();
    } catch (e) {
      approveBtn.disabled = false;
      rejectBtn.disabled = false;
      card.append(el("p", "error", e.message));
    }
  };
  actions.append(approveBtn, rejectBtn, note);
  card.append(actions);
  return card;
}

async function loadApprovals() {
  const pendingRaw = await (await api("/api/approvals?status=pending&limit=50")).json();
  const allRaw = await (await api("/api/approvals?status=all&limit=50")).json();
  const pending = Array.isArray(pendingRaw) ? pendingRaw : [];
  const all = Array.isArray(allRaw) ? allRaw : [];
  setApprovalBadge(pending.length);
  const list = $("approval-list");
  if (!list) return;
  list.replaceChildren();
  if (!pending.length) list.append(el("p", "hint", "Nothing waiting."));
  for (const a of pending) list.append(renderApprovalCard(a));
  const hist = $("approval-history");
  if (!hist) return;
  hist.replaceChildren();
  const done = all.filter((a) => a.status !== "pending");
  if (!done.length) hist.append(el("p", "hint", "No history yet."));
  for (const a of done) hist.append(renderApprovalCard(a));
}

async function loadAudit() {
  const rows = await (await api("/api/audit?limit=100")).json();
  const list = $("audit-list");
  list.replaceChildren();
  if (!rows.length) list.append(el("p", "hint", "No audit rows yet."));
  for (const r of rows) {
    const row = el("div", "audit-row");
    row.append(
      el("span", "", new Date(r.ts * 1000).toLocaleString()),
      el("span", "", r.actor || ""),
      el("span", "", r.event || ""),
      el("span", "", `${r.tool || ""} ${r.decision || ""}`),
    );
    list.append(row);
  }
}

// ---------- jobs ----------
function showView(name) {
  if (activeView === "browser" && name !== "browser") resetBrowserView();
  activeView = name;
  $("chat-view").hidden = name !== "chat";
  $("jobs-view").hidden = name !== "jobs";
  if ($("approvals-view")) $("approvals-view").hidden = name !== "approvals";
  if ($("audit-view")) $("audit-view").hidden = name !== "audit";
  if ($("files-view")) $("files-view").hidden = name !== "files";
  if ($("browser-view")) $("browser-view").hidden = name !== "browser";
  $("tab-chat").classList.toggle("active", name === "chat");
  $("tab-jobs").classList.toggle("active", name === "jobs");
  if ($("tab-approvals")) $("tab-approvals").classList.toggle("active", name === "approvals");
  if ($("tab-audit")) $("tab-audit").classList.toggle("active", name === "audit");
  if ($("tab-files")) $("tab-files").classList.toggle("active", name === "files");
  if ($("tab-browser")) $("tab-browser").classList.toggle("active", name === "browser");
  if (name === "jobs") loadJobs();
  if (name === "approvals") loadApprovals();
  if (name === "audit") loadAudit();
  if (name === "files") loadFiles();
  if (name === "browser") {
    loadBrowserStatus();
    if (browserPollTimer === null) browserPollTimer = setInterval(loadBrowserStatus, 5000);
  }
}
$("tab-chat").onclick = () => showView("chat");
$("tab-jobs").onclick = () => showView("jobs");
if ($("tab-approvals")) $("tab-approvals").onclick = () => showView("approvals");
if ($("tab-audit")) $("tab-audit").onclick = () => showView("audit");
if ($("tab-files")) $("tab-files").onclick = () => showView("files");
if ($("tab-browser")) $("tab-browser").onclick = () => showView("browser");
if ($("audit-refresh")) $("audit-refresh").onclick = () => loadAudit();
if ($("audit-export")) $("audit-export").onclick = async () => {
  const res = await api("/api/audit/export.csv");
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = "audit.csv"; a.click();
  URL.revokeObjectURL(url);
};
if ($("stop")) $("stop").onclick = async () => {
  if (currentChat == null) return;
  try {
    await api(`/api/chats/${currentChat}/stop`, { method: "POST" });
    // Stop cancels pending approvals; unlock even if approval_resolved never arrives on the SSE stream.
    setComposerLocked(false);
    loadStatus();
  } catch (e) { addMessage("error", e.message); }
};

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
  activeView = null;
  resetBrowserView();
  try { await api("/logout", { method: "POST" }); } catch (_) {}
  location.href = "/login";
};


// ---------- files ----------
function renderFileCard(ev) {
  const card = el("div", "card file-card");
  const title = ev.name || (ev.path || "").split("/").pop() || "file";
  card.append(el("strong", "", title));
  const meta = el("p", "meta", `${ev.path || ""} · ${fmtSize(ev.size || 0)}`);
  card.append(meta);
  if (ev.note) card.append(el("p", "hint", ev.note));
  const actions = el("div", "job-actions");
  const dl = el("a", "ghost", "Download");
  dl.href = `/api/files/download?path=${encodeURIComponent(ev.path || "")}`;
  dl.setAttribute("download", title);
  actions.append(dl);
  card.append(actions);
  if (ev.preview && ev.path) {
    const img = document.createElement("img");
    img.alt = title;
    img.src = `/api/files/preview?path=${encodeURIComponent(ev.path)}`;
    card.append(img);
  }
  return card;
}

function fmtSize(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

let filesPath = "";

function filesJoin(base, name) {
  if (!base) return name;
  return base.replace(/\/$/, "") + "/" + name;
}

async function loadFiles() {
  if (!$("files-list")) return;
  if ($("trash-panel")) $("trash-panel").hidden = true;
  const data = await (await api(`/api/files?path=${encodeURIComponent(filesPath)}`)).json();
  const usage = data.usage || {};
  $("files-usage").textContent = `Using ${usage.used_mb ?? "?"} / ${usage.quota_mb ?? "?"} MB (free ${usage.free_mb ?? "?"} MB)`;
  const crumb = $("files-crumb");
  crumb.replaceChildren();
  const rootBtn = el("button", "", "workspace");
  rootBtn.type = "button";
  rootBtn.onclick = () => { filesPath = ""; loadFiles(); };
  crumb.append(rootBtn);
  if (filesPath) {
    const parts = filesPath.split("/");
    let acc = "";
    for (const part of parts) {
      crumb.append(document.createTextNode(" / "));
      acc = filesJoin(acc, part);
      const btn = el("button", "", part);
      btn.type = "button";
      const target = acc;
      btn.onclick = () => { filesPath = target; loadFiles(); };
      crumb.append(btn);
    }
  }
  const list = $("files-list");
  list.replaceChildren();
  if (!data.entries.length) list.append(el("p", "hint", "Empty folder."));
  for (const entry of data.entries) {
    const row = el("div", "card file-row");
    const label = entry.name;
    const open = el("button", "ghost", label);
    open.type = "button";
    const entryPath = filesJoin(filesPath, entry.name.replace(/@$/, ""));
    if (entry.type === "dir") {
      open.onclick = () => { filesPath = entryPath; loadFiles(); };
    } else if (entry.type === "symlink") {
      open.disabled = true;
      open.title = "Symlink (not followed)";
    } else {
      open.onclick = () => {
        location.href = `/api/files/download?path=${encodeURIComponent(entryPath)}`;
      };
    }
    const meta = el("span", "meta", entry.type === "file" ? fmtSize(entry.size || 0) : entry.type);
    const origin = el("span", "origin", entry.origin || "unknown");
    const del = el("button", "ghost danger", "Delete");
    del.type = "button";
    del.onclick = async () => {
      if (!confirm(`Move "${entryPath}" to trash?`)) return;
      try {
        await api(`/api/files?path=${encodeURIComponent(entryPath)}`, { method: "DELETE" });
        loadFiles();
      } catch (e) { alert(e.message); }
    };
    row.append(open, meta, origin);
    if (entry.type !== "symlink") {
      const actions = el("div", "job-actions");
      actions.append(del);
      row.append(actions);
    }
    list.append(row);
  }
}

async function loadTrash() {
  $("trash-panel").hidden = false;
  const rows = await (await api("/api/trash")).json();
  const list = $("trash-list");
  list.replaceChildren();
  if (!rows.length) list.append(el("p", "hint", "Trash is empty."));
  for (const row of rows) {
    const card = el("div", "card");
    card.append(el("strong", "", row.original_path));
    card.append(el("p", "meta", `${fmtSize(row.size)} · ${fmtTime(row.deleted)} · ${row.deleted_by}`));
    const btn = el("button", "ghost", "Restore");
    btn.type = "button";
    btn.onclick = async () => {
      try {
        await api(`/api/trash/${row.id}/restore`, { method: "POST", body: "{}" });
        loadTrash();
        loadFiles();
      } catch (e) { alert(e.message); }
    };
    card.append(btn);
    list.append(card);
  }
}

function uploadFiles(fileList, { destDir, intoComposer }) {
  const files = Array.from(fileList || []);
  if (!files.length) return;
  const progress = $("files-progress");
  (async () => {
    for (const file of files) {
      const day = new Date().toISOString().slice(0, 10);
      const dest = intoComposer
        ? `uploads/${day}/${file.name}`
        : (destDir ? filesJoin(destDir, file.name) : file.name);
      if (progress) {
        progress.hidden = false;
        progress.textContent = `Uploading ${file.name}…`;
      }
      await new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open("PUT", `/api/files/content?path=${encodeURIComponent(dest)}`);
        xhr.setRequestHeader("X-CSRF-Token", csrf);
        xhr.setRequestHeader("X-Overwrite", "0");
        xhr.upload.onprogress = (ev) => {
          if (progress && ev.lengthComputable) {
            progress.textContent = `Uploading ${file.name}… ${Math.round(100 * ev.loaded / ev.total)}%`;
          }
        };
        xhr.onload = () => {
          if (xhr.status >= 200 && xhr.status < 300) {
            if (intoComposer) {
              const size = fmtSize(file.size);
              const marker = `[Attached: ${dest} (${size})]`;
              const cur = $("input").value;
              $("input").value = cur ? (cur + "\n" + marker) : marker;
              autosize();
            }
            resolve();
          } else {
            let msg = xhr.statusText;
            try { msg = JSON.parse(xhr.responseText).detail || msg; } catch (_) {}
            reject(new Error(typeof msg === "string" ? msg : JSON.stringify(msg)));
          }
        };
        xhr.onerror = () => reject(new Error("upload failed"));
        xhr.send(file);
      });
    }
    if (progress) { progress.hidden = true; progress.textContent = ""; }
    if (!intoComposer) loadFiles();
  })().catch((e) => {
    if (progress) { progress.hidden = false; progress.textContent = e.message; }
    else alert(e.message);
  });
}

if ($("files-refresh")) $("files-refresh").onclick = () => loadFiles();
if ($("files-upload-btn")) $("files-upload-btn").onclick = () => $("files-upload-input").click();
if ($("files-upload-input")) $("files-upload-input").onchange = (e) => {
  uploadFiles(e.target.files, { destDir: filesPath, intoComposer: false });
  e.target.value = "";
};
if ($("files-mkdir-btn")) $("files-mkdir-btn").onclick = async () => {
  const name = prompt("Folder name");
  if (!name) return;
  const dest = filesJoin(filesPath, name);
  try {
    await api("/api/files/mkdir", { method: "POST", body: JSON.stringify({ path: dest }) });
    loadFiles();
  } catch (e) { alert(e.message); }
};
if ($("files-trash-btn")) $("files-trash-btn").onclick = () => loadTrash();
if ($("attach")) $("attach").onclick = () => $("attach-input").click();
if ($("attach-input")) $("attach-input").onchange = (e) => {
  uploadFiles(e.target.files, { intoComposer: true });
  e.target.value = "";
};

// ---------- browser ----------
let browserState = null;
let browserStateKey = "";
let browserStateVersion = 0;
let browserStatusLoad = 0;
let browserShotLoad = 0;
let browserShotPending = false;
let browserThumbnailURL = "";
let browserPollTimer = null;

function browserCanRefresh() {
  return !!(browserState && browserState.enabled && browserState.reachable && browserState.mode === "agent");
}

function releaseBrowserThumbnail() {
  const image = $("browser-thumbnail");
  if (image) {
    image.hidden = true;
    image.onerror = null;
    image.removeAttribute("src");
  }
  if (browserThumbnailURL) URL.revokeObjectURL(browserThumbnailURL);
  browserThumbnailURL = "";
}

function invalidateBrowserThumbnail() {
  ++browserShotLoad;
  browserShotPending = false;
  releaseBrowserThumbnail();
}

function browserError(message) {
  $("browser-error").textContent = message;
  $("browser-error").hidden = !message;
}

function renderBrowserState() {
  const state = browserState;
  $("browser-details").hidden = !state || !state.enabled || !state.reachable;
  $("browser-refresh").disabled = !browserCanRefresh() || browserShotPending;
  $("browser-refresh").textContent = browserShotPending ? "Refreshing…" : "Refresh";
  $("browser-status").textContent = !state ? "Loading browser status…"
    : !state.enabled ? "Browser is off."
    : !state.reachable ? "Browser is on but unavailable."
    : "Browser is on.";
  $("browser-tabs").replaceChildren();
  $("browser-mode").textContent = "";
  $("browser-title").textContent = "";
  $("browser-address").textContent = "";
  if (!state || !state.enabled || !state.reachable) return;
  $("browser-mode").textContent = state.mode === "agent" ? "Agent mode" : state.mode === "user" ? "User mode" : "Mode unavailable";
  $("browser-title").textContent = typeof state.title === "string" && state.title ? state.title : "No active page";
  $("browser-address").textContent = typeof state.url === "string" && state.url ? state.url : "No address";
  const tabs = Array.isArray(state.tabs) ? state.tabs : [];
  for (const tab of tabs) {
    const row = el("li", "browser-tab-row" + (tab.active ? " active" : ""));
    if (tab.active) row.append(el("span", "badge", "Active"));
    row.append(el("strong", "browser-tab-title", typeof tab.title === "string" && tab.title ? tab.title : "Untitled tab"));
    row.append(el("span", "browser-tab-address", typeof tab.url === "string" ? tab.url : ""));
    $("browser-tabs").append(row);
  }
  if (!tabs.length) $("browser-tabs").append(el("li", "hint", "No tabs open."));
  $("browser-shot-note").textContent = browserShotPending ? "Getting the current thumbnail…"
    : state.mode === "user" ? "Thumbnail refresh is paused while you use the browser."
    : browserThumbnailURL ? "Thumbnail from the latest refresh." : "Choose Refresh to get the current thumbnail.";
}

function resetBrowserView(stopPolling = true) {
  if (stopPolling && browserPollTimer !== null) {
    clearInterval(browserPollTimer);
    browserPollTimer = null;
  }
  ++browserStatusLoad;
  ++browserStateVersion;
  browserState = null;
  browserStateKey = "";
  invalidateBrowserThumbnail();
  browserError("");
  renderBrowserState();
}

async function loadBrowserStatus() {
  if (activeView !== "browser") return;
  const load = ++browserStatusLoad;
  try {
    const state = await (await api("/api/browser/status")).json();
    if (load !== browserStatusLoad || activeView !== "browser") return;
    const key = JSON.stringify(state);
    if (key !== browserStateKey) {
      ++browserStateVersion;
      invalidateBrowserThumbnail();
    }
    browserStateKey = key;
    browserState = state;
    browserError("");
    renderBrowserState();
  } catch (e) {
    if (load !== browserStatusLoad || activeView !== "browser") return;
    resetBrowserView(false);
    $("browser-status").textContent = "Browser status is unavailable.";
    browserError(e.message || "Could not load browser status.");
  }
}

async function refreshBrowserThumbnail() {
  if (activeView !== "browser" || !browserCanRefresh() || browserShotPending) return;
  const load = ++browserShotLoad;
  const version = browserStateVersion;
  browserShotPending = true;
  browserError("");
  renderBrowserState();
  try {
    const blob = await (await api("/api/browser/screenshot", { method: "POST" })).blob();
    if (load !== browserShotLoad || version !== browserStateVersion || activeView !== "browser") return;
    if (blob.type !== "image/png") throw new Error("Screenshot response was not a PNG.");
    const url = URL.createObjectURL(blob);
    releaseBrowserThumbnail();
    browserThumbnailURL = url;
    const image = $("browser-thumbnail");
    image.onerror = () => {
      if (browserThumbnailURL !== url) return;
      releaseBrowserThumbnail();
      browserError("Could not display the browser thumbnail.");
      renderBrowserState();
    };
    image.src = url;
    image.hidden = false;
  } catch (e) {
    if (load !== browserShotLoad || version !== browserStateVersion || activeView !== "browser") return;
    releaseBrowserThumbnail();
    if (e.message === "user_mode") {
      ++browserStateVersion;
      invalidateBrowserThumbnail();
      browserState = { ...browserState, mode: "user" };
      browserStateKey = JSON.stringify(browserState);
      renderBrowserState();
      browserError("Thumbnail refresh is paused while you use the browser.");
      await loadBrowserStatus();
      return;
    }
    browserError(e.message || "Could not refresh the browser thumbnail.");
  } finally {
    if (load === browserShotLoad && version === browserStateVersion && activeView === "browser") {
      browserShotPending = false;
      renderBrowserState();
    }
  }
}

if ($("browser-refresh")) $("browser-refresh").onclick = refreshBrowserThumbnail;
window.addEventListener("pagehide", () => {
  activeView = null;
  if (browserPollTimer !== null) clearInterval(browserPollTimer);
  browserPollTimer = null;
  ++browserStatusLoad;
  ++browserStateVersion;
  invalidateBrowserThumbnail();
});

// ---------- start ----------
(async () => {
  await loadStatus();
  const chats = await loadChats();
  if (chats.length) openChat(chats[0].id);
})();
