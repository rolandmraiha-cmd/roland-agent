const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// A small DOM fixture runs the actual shipped script without packages or network access.
class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase();
    this.children = []; this.textContent = ''; this.value = ''; this.disabled = false;
    this.style = {}; this.listeners = {}; this.scrollHeight = 100; this.scrollTop = 0;
    this.classes = new Set();
    this.classList = {
      add: (name) => this.classes.add(name), remove: (name) => this.classes.delete(name),
      contains: (name) => this.classes.has(name), toggle: () => {},
    };
  }
  append(...nodes) { for (const node of nodes) { node.parent = this; this.children.push(node); } }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  querySelector() { return this.children[0]; }
  setAttribute() {}
  removeAttribute(name) { delete this[name]; }
  set innerHTML(_) { throw new Error('Use textContent for dynamic text'); }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  focus() { this.focused = true; }
  remove() { this.parent.children = this.parent.children.filter((node) => node !== this); }
  before(node) { this.parent.append(node); }
  after(node) { this.parent.append(node); }
}
function fixture() {
  const elements = new Map();
  const get = (id) => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const timers = new Map();
  const pageEvents = {}, createdURLs = [], revokedURLs = [];
  let nextTimer = 1;
  const context = vm.createContext({
    document: { getElementById: get, createElement: (tag) => new Element(tag) },
    window: { addEventListener: (name, fn) => { pageEvents[name] = fn; } },
    URL: {
      createObjectURL: (blob) => { const url = `blob:fixture-${createdURLs.length}`; createdURLs.push({ url, blob }); return url; },
      revokeObjectURL: (url) => { revokedURLs.push(url); },
    },
    TextDecoder, matchMedia: () => ({ matches: false }),
    location: {}, alert() {}, confirm: () => true,
    Date,
    setInterval: (fn) => { const id = nextTimer++; timers.set(id, fn); return id; },
    clearInterval: (id) => { timers.delete(id); },
    setTimeout: (fn) => { const id = nextTimer++; queueMicrotask(() => { if (timers.has(id)) { timers.delete(id); fn(); } }); return id; },
    clearTimeout: (id) => { timers.delete(id); },
    fetch: () => { throw new Error('Unexpected fetch'); },
  });
  const source = fs.readFileSync(path.join(__dirname, '../../agent/web/static/app.js'), 'utf8');
  vm.runInContext(source.split('// ---------- start ----------')[0], context);
  vm.runInContext('loadChats = async () => []; loadStatus = async () => {};', context);
  return { context, get, pageEvents, createdURLs, revokedURLs, run: (code) => vm.runInContext(code, context) };
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const finishedStream = { body: { getReader: () => ({ read: async () => ({ done: true }) }) } };

function browserFixture() {
  const f = fixture();
  f.state = {
    enabled: true, reachable: true, mode: 'agent', title: 'Fixture blog',
    url: 'https://fixture.example/blog',
    tabs: [{ id: 't1', title: 'Fixture blog', url: 'https://fixture.example/blog', active: true }],
  };
  f.calls = [];
  f.context.api = async (url, options = {}) => {
    f.calls.push({ url, options });
    if (url === '/api/browser/status') return { json: async () => f.state };
    if (url === '/api/browser/screenshot') return { blob: async () => ({ type: 'image/png' }) };
    if (url === '/logout') return {};
    throw new Error(`Unexpected browser API call: ${url}`);
  };
  f.run('activeView = "browser"');
  return f;
}

test('Browser tab follows Files and renders titles and addresses as text, never links', async () => {
  const html = fs.readFileSync(path.join(__dirname, '../../agent/web/static/index.html'), 'utf8');
  assert.match(html, /id="tab-files"[^]*?id="tab-browser"/);
  assert.match(html, /<dd id="browser-address"><\/dd>/);
  const f = browserFixture();
  const title = '<img src=x onerror=alert(1)>', url = 'javascript:alert("fixture")';
  f.state.title = title;
  f.state.url = url;
  f.state.tabs = [{ id: 't1', title, url, active: true }];
  await f.run('loadBrowserStatus()');
  assert.equal(f.get('browser-status').textContent, 'Browser is on.');
  assert.equal(f.get('browser-mode').textContent, 'Agent mode');
  assert.equal(f.get('browser-title').textContent, title);
  assert.equal(f.get('browser-address').textContent, url);
  const row = f.get('browser-tabs').children[0];
  assert.equal(row.children[1].textContent, title);
  assert.equal(row.children[2].textContent, url);
  assert.equal(row.children.some((node) => node.tagName === 'A'), false);
  assert.equal(f.get('browser-address').href, undefined);
  assert.equal(f.get('browser-refresh').disabled, false);
});

test('Browser off shows one sentence and hides browser details', async () => {
  const f = browserFixture();
  f.state = { enabled: false };
  await f.run('loadBrowserStatus()');
  assert.equal(f.get('browser-status').textContent, 'Browser is off.');
  assert.equal(f.get('browser-details').hidden, true);
  assert.equal(f.get('browser-refresh').disabled, true);
  await f.run('refreshBrowserThumbnail()');
  assert.equal(f.calls.length, 1);
});

test('thumbnail uses POST and blob URLs, revoking replacements and on tab exit', async () => {
  const f = browserFixture();
  await f.run('loadBrowserStatus()');
  await f.get('browser-refresh').onclick();
  assert.equal(f.calls[1].url, '/api/browser/screenshot');
  assert.equal(f.calls[1].options.method, 'POST');
  assert.equal(f.get('browser-thumbnail').src, 'blob:fixture-0');
  assert.equal(f.get('browser-thumbnail').hidden, false);
  await f.run('refreshBrowserThumbnail()');
  assert.deepEqual(f.revokedURLs, ['blob:fixture-0']);
  // A poll with the same status keeps the current image.
  await f.run('loadBrowserStatus()');
  assert.equal(f.get('browser-thumbnail').src, 'blob:fixture-1');
  f.run('showView("chat")');
  assert.deepEqual(f.revokedURLs, ['blob:fixture-0', 'blob:fixture-1']);
  assert.equal(f.get('browser-thumbnail').src, undefined);
  assert.equal(f.get('browser-thumbnail').hidden, true);
});

test('user mode and unavailable or unknown status disable refresh and clear thumbnails', async () => {
  const f = browserFixture();
  await f.run('loadBrowserStatus();');
  await f.run('refreshBrowserThumbnail()');
  f.state = { ...f.state, mode: 'user' };
  await f.run('loadBrowserStatus()');
  assert.equal(f.get('browser-mode').textContent, 'User mode');
  assert.equal(f.get('browser-refresh').disabled, true);
  assert.equal(f.get('browser-thumbnail').hidden, true);
  assert.deepEqual(f.revokedURLs, ['blob:fixture-0']);
  f.state = { ...f.state, mode: null };
  await f.run('loadBrowserStatus()');
  assert.equal(f.get('browser-refresh').disabled, true);
  f.state = { enabled: true, reachable: false };
  await f.run('loadBrowserStatus()');
  assert.equal(f.get('browser-status').textContent, 'Browser is on but unavailable.');
  assert.equal(f.get('browser-details').hidden, true);
  assert.equal(f.get('browser-address').textContent, '');
});

test('late thumbnail cannot restore an image after leaving the Browser tab', async () => {
  const f = browserFixture(), shot = deferred();
  await f.run('loadBrowserStatus()');
  f.context.api = () => shot.promise;
  const refreshing = f.run('refreshBrowserThumbnail()');
  assert.equal(f.get('browser-refresh').disabled, true);
  f.run('showView("chat")');
  shot.resolve({ blob: async () => ({ type: 'image/png' }) });
  await refreshing;
  assert.equal(f.createdURLs.length, 0);
  assert.equal(f.get('browser-thumbnail').hidden, true);
});

test('thumbnail errors render as text and refuse non-PNG blobs', async () => {
  const f = browserFixture();
  await f.run('loadBrowserStatus()');
  f.context.api = async () => ({ blob: async () => ({ type: 'image/svg+xml' }) });
  await f.run('refreshBrowserThumbnail()');
  assert.equal(f.createdURLs.length, 0);
  assert.match(f.get('browser-error').textContent, /not a PNG/);
  f.context.api = async () => { throw new Error('<script>fixture</script>'); };
  await f.run('refreshBrowserThumbnail()');
  assert.equal(f.get('browser-error').textContent, '<script>fixture</script>');
  assert.equal(f.get('browser-refresh').disabled, false);
});

test('logout and page exit revoke thumbnails', async () => {
  for (const exit of ['logout', 'pagehide']) {
    const f = browserFixture();
    await f.run('loadBrowserStatus()');
    await f.run('refreshBrowserThumbnail()');
    if (exit === 'logout') await f.get('logout').onclick();
    else f.pageEvents.pagehide();
    assert.deepEqual(f.revokedURLs, ['blob:fixture-0']);
    assert.equal(f.get('browser-thumbnail').hidden, true);
  }
});

test('a mode change during capture reloads status and pauses Refresh', async () => {
  const f = browserFixture();
  await f.run('loadBrowserStatus()');
  f.state = { ...f.state, mode: 'user' };
  f.context.api = async (url) => {
    if (url === '/api/browser/screenshot') throw new Error('user_mode');
    assert.equal(url, '/api/browser/status');
    return { json: async () => f.state };
  };
  await f.run('refreshBrowserThumbnail()');
  assert.equal(f.get('browser-mode').textContent, 'User mode');
  assert.equal(f.get('browser-refresh').disabled, true);
  assert.notEqual(f.get('browser-error').textContent, 'user_mode');
});

test('Browser polling starts on tab entry and stops on exit', async () => {
  const f = browserFixture();
  f.run('activeView = "chat"; showView("browser")');
  await new Promise((resolve) => setImmediate(resolve));
  assert.notEqual(f.run('browserPollTimer'), null);
  f.run('showView("chat")');
  assert.equal(f.run('browserPollTimer'), null);
});

test('first send reserves composer before chat creation and blocks navigation/double send', async () => {
  const f = fixture(), creation = deferred();
  const calls = [];
  f.context.api = async (url) => {
    calls.push(url);
    if (url === '/api/chats') return creation.promise;
    return finishedStream;
  };
  const first = f.run('send("hello")');
  assert.equal(f.run('sending'), true);
  assert.equal(f.get('new-chat').disabled, true);
  await f.run('send("duplicate")');
  await f.run('openChat(99)');
  await f.run('newChat()');
  assert.deepEqual(calls, ['/api/chats']);
  creation.resolve({ json: async () => ({ id: 7 }) });
  await first;
  assert.deepEqual(calls.slice(0, 2), ['/api/chats', '/api/chats/7/send']);
  // Empty stream may refetch history to recover a server-saved reply.
  assert.ok(calls.length <= 3);
  assert.equal(f.run('sending'), false);
  assert.equal(f.get('send').disabled, false);
});

test('failed chat creation restores draft and unlocks composer', async () => {
  const f = fixture();
  f.context.api = async () => { throw new Error('Offline'); };
  await f.run('send("keep this draft")');
  assert.equal(f.get('input').value, 'keep this draft');
  assert.equal(f.run('sending'), false);
  assert.equal(f.run('currentChat'), null);
  assert.match(f.get('messages').children[0].children[0].textContent, /Offline/);
});

test('failed creation does not overwrite newly typed draft', async () => {
  const f = fixture(), creation = deferred();
  f.context.api = () => creation.promise;
  const sending = f.run('send("old draft")');
  f.get('input').value = 'new draft';
  creation.reject(new Error('Offline'));
  await sending;
  assert.equal(f.get('input').value, 'new draft');
});

test('out of order history responses cannot replace selected chat', async () => {
  const f = fixture(), older = deferred();
  f.context.api = async (url) => url.includes('/1/') ? older.promise : {
    json: async () => ({ messages: [{ role: 'assistant', content: 'newer chat' }] }),
  };
  const first = f.run('openChat(1)');
  await f.run('openChat(2)');
  older.resolve({ json: async () => ({ messages: [{ role: 'assistant', content: 'older chat' }] }) });
  await first;
  assert.equal(f.run('currentChat'), 2);
  assert.equal(f.get('messages').children[0].children[0].textContent, 'newer chat');
});

test('pending history cannot clear messages while sending', async () => {
  const f = fixture(), history = deferred(), stream = deferred();
  f.context.api = async (url) => url.endsWith('/messages') ? history.promise : stream.promise;
  const opening = f.run('openChat(1)');
  const sending = f.run('send("live message")');
  history.resolve({ json: async () => ({ messages: [] }) });
  await opening;
  assert.equal(f.get('messages').children[0].children[0].textContent, 'live message');
  stream.resolve(finishedStream);
  await sending;
});

test('IME Enter does not submit while normal desktop Enter still does', () => {
  const f = fixture();
  let submissions = 0, prevented = 0;
  f.get('composer').requestSubmit = () => submissions++;
  const keydown = f.get('input').listeners.keydown;
  keydown({ key: 'Enter', isComposing: true, preventDefault: () => prevented++ });
  assert.equal(submissions, 0);
  keydown({ key: 'Enter', isComposing: false, preventDefault: () => prevented++ });
  assert.equal(submissions, 1);
  assert.equal(prevented, 1);
});

test('new-chat creation blocks sends until its destination is known', async () => {
  const f = fixture(), creation = deferred(), calls = [];
  f.context.api = async (url) => {
    calls.push(url);
    return url === '/api/chats' ? creation.promise : { json: async () => ({ messages: [] }) };
  };
  const creating = f.run('newChat()');
  await f.run('send("must wait")');
  await f.run('newChat()');
  assert.deepEqual(calls, ['/api/chats']);
  creation.resolve({ json: async () => ({ id: 4 }) });
  await creating;
  assert.equal(f.run('currentChat'), 4);
  assert.equal(f.run('sending'), false);
});

test('new-chat failure unlocks the composer and shows an error', async () => {
  const f = fixture();
  f.context.api = async () => { throw new Error('Offline'); };
  await f.run('newChat()');
  assert.equal(f.run('sending'), false);
  assert.equal(f.get('new-chat').disabled, false);
  assert.match(f.get('messages').children[0].children[0].textContent, /Offline/);
});

test('coarse pointer Enter inserts lines, Ctrl/Cmd+Enter sends, Shift+Enter preserves lines', () => {
  const f = fixture();
  f.context.matchMedia = () => ({ matches: true });
  let submissions = 0;
  f.get('composer').requestSubmit = () => submissions++;
  const keydown = f.get('input').listeners.keydown;
  keydown({ key: 'Enter', preventDefault() { assert.fail('plain mobile Enter'); } });
  keydown({ key: 'Enter', ctrlKey: true, preventDefault() {} });
  keydown({ key: 'Enter', metaKey: true, preventDefault() {} });
  keydown({ key: 'Enter', shiftKey: true, ctrlKey: true, preventDefault() { assert.fail('Shift+Enter'); } });
  assert.equal(submissions, 2);
});

test('finishing a new-chat history load must not unlock an active send', async () => {
  const f = fixture(), history = deferred(), stream = deferred(), loading = deferred();
  f.context.api = async (url) => {
    if (url === '/api/chats') return { json: async () => ({ id: 5 }) };
    if (url.endsWith('/messages')) { loading.resolve(); return history.promise; }
    return stream.promise;
  };
  const creating = f.run('newChat()');
  await loading.promise;
  const sending = f.run('send("active")');
  history.resolve({ json: async () => ({ messages: [] }) });
  await creating;
  assert.equal(f.run('sending'), true);
  assert.equal(f.get('send').disabled, true);
  stream.resolve(finishedStream);
  await sending;
});

test('opening another chat immediately clears the previous conversation', async () => {
  const f = fixture(), history = deferred();
  f.run('addMessage("assistant", "previous conversation")');
  f.context.api = () => history.promise;
  const opening = f.run('openChat(8)');
  assert.equal(f.get('messages').children.length, 0);
  history.resolve({ json: async () => ({ messages: [{ role: 'assistant', content: 'selected conversation' }] }) });
  await opening;
  assert.equal(f.get('messages').children[0].children[0].textContent, 'selected conversation');
});

test('Safari IME Enter (keyCode 229) does not submit', () => {
  const f = fixture();
  let submissions = 0;
  f.get('composer').requestSubmit = () => submissions++;
  f.get('input').listeners.keydown({ key: 'Enter', isComposing: false, keyCode: 229, preventDefault() {} });
  assert.equal(submissions, 0);
});

test('failed history load shows an error instead of an empty chat', async () => {
  const f = fixture();
  f.context.api = async () => { throw new Error('Server error'); };
  await f.run('openChat(3)');
  assert.equal(f.run('currentChat'), 3);
  assert.match(f.get('messages').children[0].children[0].textContent, /Couldn't load this chat: Server error/);
});

test('old-origin jobs say the origin is unknown', async () => {
  const f = fixture();
  f.context.api = async () => ({ json: async () => ({
    timezone: 'Europe/Helsinki', facts: [], runs: [],
    jobs: [{ id: 1, name: 'Legacy', cron: '* * * * *', prompt: 'Check',
             origin: 'old', approved: false }],
  }) });
  await f.run('loadJobs()');
  const status = f.get('job-list').children[0].children[2].textContent;
  assert.equal(status, "Where this job came from wasn't recorded (it was made before v1 tracked that), so the agent may have made it. Waiting for your OK: read what it does, then approve or delete it.");
  f.context.api = async () => ({ json: async () => ({
    timezone: 'Europe/Helsinki', facts: [], runs: [],
    jobs: [{ id: 2, name: 'Agent', cron: '* * * * *', prompt: 'Check',
             origin: 'agent', approved: false }],
  }) });
  await f.run('loadJobs()');
  assert.match(f.get('job-list').children[0].children[2].textContent, /^The agent made this job\. /);
});

test('deleting the open chat suppresses its pending load error', async () => {
  const f = fixture(), history = deferred();
  // Restore the shipped list loader that the fixture normally replaces with a stub.
  const source = fs.readFileSync(path.join(__dirname, '../../agent/web/static/app.js'), 'utf8');
  const loader = source.slice(source.indexOf('async function loadChats()'),
                              source.indexOf('// ---------- chat ----------'));
  vm.runInContext(loader, f.context);
  let chats = [{ id: 3, title: 'Pending' }];
  f.context.api = async (url, options = {}) => {
    if (url.endsWith('/messages')) return history.promise;
    if (options.method === 'DELETE') { chats = []; return {}; }
    return { json: async () => chats };
  };
  const opening = f.run('openChat(3)');
  await f.run('loadChats()');
  await f.get('chat-list').children[0].children[1].onclick();
  history.reject(new Error('no such chat'));
  await opening;
  assert.equal(f.run('currentChat'), null);
  assert.equal(f.get('messages').children.length, 0);
  assert.equal(f.get('title').textContent, 'Chat');
});



function sseStream(events, { crlf = false, keepalives = [] } = {}) {
  const sep = crlf ? "\r\n\r\n" : "\n\n";
  const pieces = [];
  for (const item of keepalives) pieces.push(`: keepalive${sep}`);
  for (const ev of events) pieces.push(`data: ${JSON.stringify(ev)}${sep}`);
  let i = 0;
  const encoder = new TextEncoder();
  return {
    body: {
      getReader: () => ({
        read: async () => {
          if (i >= pieces.length) return { done: true };
          const value = encoder.encode(pieces[i++]);
          return { value, done: false };
        },
      }),
    },
  };
}

test('done event replaces thinking placeholder without any text deltas', async () => {
  const f = fixture();
  f.run('currentChat = 1');
  f.context.api = async (url) => {
    if (url.endsWith('/send')) {
      return sseStream([{ type: "done", reply: "Final answer from local model" }, { type: "end" }]);
    }
    return { json: async () => ([]) };
  };
  await f.run('send("hi")');
  const msgs = f.get('messages').children;
  assert.equal(msgs[0].children[0].textContent, "hi");
  assert.equal(msgs[1].children[0].textContent, "Final answer from local model");
  assert.equal(msgs[1].classes.has("typing"), false);
  assert.equal(f.run('sending'), false);
});

test('done overwrites streamed text with the authoritative reply', async () => {
  const f = fixture();
  f.run('currentChat = 2');
  f.context.api = async (url) => url.endsWith('/send')
    ? sseStream([
        { type: "text", text: "partial" },
        { type: "done", reply: "complete reply" },
        { type: "end" },
      ])
    : { json: async () => ([]) };
  await f.run('send("go")');
  assert.equal(f.get('messages').children[1].children[0].textContent, "complete reply");
  assert.equal(f.get('messages').children[1].classes.has("typing"), false);
});

test('CRLF SSE framing and keepalive comments still deliver done', async () => {
  const f = fixture();
  f.run('currentChat = 3');
  f.context.api = async (url) => url.endsWith('/send')
    ? sseStream([{ type: "done", reply: "crlf ok" }, { type: "end" }], { crlf: true, keepalives: [1] })
    : { json: async () => ([]) };
  await f.run('send("ping")');
  assert.equal(f.get('messages').children[1].children[0].textContent, "crlf ok");
});

test('stream end with only thinking removes the placeholder bubble', async () => {
  const f = fixture();
  f.run('currentChat = 4');
  f.context.api = async (url) => url.endsWith('/send')
    ? finishedStream
    : { json: async () => ({ messages: [] }) };
  await f.run('send("empty")');
  assert.equal(f.get('messages').children.length, 1); // user only; thinking bubble removed
  assert.equal(f.get('messages').children[0].children[0].textContent, "empty");
});


test('error event clears thinking and shows the error', async () => {
  const f = fixture();
  f.run('currentChat = 5');
  f.context.api = async (url) => url.endsWith('/send')
    ? sseStream([
        { type: "error", message: "RemoteProtocolError: Server disconnected without sending a response." },
        { type: "end" },
      ])
    : { json: async () => ({ messages: [] }) };
  await f.run('send("are you shore")');
  const msgs = f.get('messages').children;
  assert.equal(msgs.length, 2); // user + error (empty thinking assistant removed)
  assert.equal(msgs[0].children[0].textContent, "are you shore");
  assert.match(msgs[1].textContent, /RemoteProtocolError/);
  assert.equal(msgs.every((m) => !String(m.textContent).startsWith("thinking")), true);
});

test('stream disconnect recovers saved assistant reply from history', async () => {
  const f = fixture();
  f.run('currentChat = 6');
  f.context.api = async (url) => {
    if (url.endsWith('/send')) return finishedStream;
    if (url.endsWith('/messages')) {
      return { json: async () => ({ messages: [
        { role: "user", content: "hi" },
        { role: "assistant", content: "saved after disconnect" },
      ] }) };
    }
    return { json: async () => ([]) };
  };
  await f.run('send("hi")');
  assert.equal(f.get('messages').children[1].children[0].textContent, "saved after disconnect");
});


test('empty done.reply does not wipe already-streamed text', async () => {
  const f = fixture();
  f.run('currentChat = 7');
  f.context.api = async (url) => url.endsWith('/send')
    ? sseStream([
        { type: "text", text: "partial stream" },
        { type: "done", reply: "" },
        { type: "end" },
      ])
    : { json: async () => ({ messages: [] }) };
  await f.run('send("go")');
  assert.equal(f.get('messages').children[1].children[0].textContent, "partial stream");
});

test('error with prior history does not resurrect an older assistant reply', async () => {
  const f = fixture();
  f.run('currentChat = 8');
  f.context.api = async (url) => {
    if (url.endsWith('/send')) {
      return sseStream([
        { type: "error", message: "RemoteProtocolError: disconnected" },
        { type: "end" },
      ]);
    }
    if (url.endsWith('/messages')) {
      // History still only has the previous turn — this turn never saved an assistant.
      return { json: async () => ({ messages: [
        { role: "user", content: "earlier" },
        { role: "assistant", content: "old reply that must not resurface" },
        { role: "user", content: "new question" },
      ] }) };
    }
    return { json: async () => ({ messages: [] }) };
  };
  await f.run('send("new question")');
  const texts = f.get('messages').children.map((m) => m.textContent || (m.children[0] && m.children[0].textContent) || "");
  assert.equal(texts.some((t) => t.includes("old reply that must not resurface")), false);
  assert.equal(texts.some((t) => /RemoteProtocolError/.test(t)), true);
});

test('soft-recover ignores assistant messages from before this turn', async () => {
  const f = fixture();
  f.run('currentChat = 9');
  f.context.api = async (url) => {
    if (url.endsWith('/send')) return finishedStream;
    if (url.endsWith('/messages')) {
      // Disconnect before this turn's assistant was saved — only prior assistant exists.
      return { json: async () => ({ messages: [
        { role: "user", content: "earlier" },
        { role: "assistant", content: "stale prior reply" },
        { role: "user", content: "latest" },
      ] }) };
    }
    return { json: async () => ({ messages: [] }) };
  };
  await f.run('send("latest")');
  const msgs = f.get('messages').children;
  assert.equal(msgs.length, 1); // user only; thinking removed, stale not resurrected
  assert.equal(msgs[0].children[0].textContent, "latest");
});

test('soft-recover still picks assistant that follows this turn\'s user message', async () => {
  const f = fixture();
  f.run('currentChat = 10');
  f.context.api = async (url) => {
    if (url.endsWith('/send')) return finishedStream;
    if (url.endsWith('/messages')) {
      return { json: async () => ({ messages: [
        { role: "user", content: "earlier" },
        { role: "assistant", content: "stale prior reply" },
        { role: "user", content: "hi" },
        { role: "assistant", content: "fresh reply for this turn" },
      ] }) };
    }
    return { json: async () => ({ messages: [] }) };
  };
  await f.run('send("hi")');
  assert.equal(f.get('messages').children[1].children[0].textContent, "fresh reply for this turn");
});

// ---------- M3 approval gate UI ----------
test('approval card renders summary via textContent and posts args_hash', async () => {
  const f = fixture();
  const posts = [];
  f.context.api = async (url, options = {}) => {
    posts.push({ url, body: options.body });
    if (url.includes('/approve')) return { json: async () => ({ status: 'approved' }) };
    if (url.startsWith('/api/approvals')) return { json: async () => [] };
    if (url === '/api/status') return { json: async () => ({ csrf: 'x', name: 'T', model: 'm', calls_left: 1, daily_limit: 1, pending_approvals: 0 }) };
    return { json: async () => ({}) };
  };
  f.context.loadApprovals = async () => {};
  f.context.loadStatus = async () => {};
  const approval = {
    id: 'appr1', tool: 'forget', category: 'delete', summary: 'Forget fact 9',
    details: { fact_id: 9 }, args_hash: 'a'.repeat(64), needs_confirm: false,
    status: 'pending', args: { fact_id: 9 }, tainted: false, expires: Date.now() / 1000 + 60,
  };
  const card = f.run('renderApprovalCard(' + JSON.stringify(approval) + ')');
  const summary = card.children.find((c) => c.className === 'summary');
  assert.ok(summary);
  assert.equal(summary.textContent, 'Forget fact 9');
  const approveBtn = card.children.find((c) => c.className === 'actions').children[0];
  await approveBtn.onclick();
  await new Promise((r) => setImmediate(r));
  assert.ok(posts.some((p) => p.url.includes('/api/approvals/appr1/approve')));
  const body = JSON.parse(posts.find((p) => p.url.includes('/approve')).body);
  assert.equal(body.args_hash, 'a'.repeat(64));
});

test('browser approval card shows its screenshot only from the preview route', () => {
  const f = fixture();
  const base = {
    id: 'appr2', tool: 'browser_click', category: 'payment', summary: 'Click “Place order”',
    details: {}, args_hash: 'b'.repeat(64), needs_confirm: true, status: 'pending',
    args: { ref: 'e5' }, tainted: true, expires: Date.now() / 1000 + 60,
  };
  const shot = '/api/files/preview?path=screenshots/approval-appr2.png';
  const card = f.run('renderApprovalCard(' + JSON.stringify({ ...base, screenshot_url: shot }) + ')');
  const img = card.children.find((c) => c.className === 'shot');
  assert.ok(img);
  assert.equal(img.src, shot);
  assert.ok(img.alt.length > 0);
  for (const bad of [null, 'https://evil.example/x.png', '//evil.example/x.png', 'javascript:alert(1)',
    '/api/files/preview?path=uploads/x.png', { path: 'x' }]) {
    const other = f.run('renderApprovalCard(' + JSON.stringify({ ...base, screenshot_url: bad }) + ')');
    assert.equal(other.children.find((c) => c.className === 'shot'), undefined);
  }
});

test('needs_confirm requires a second tap', async () => {
  const f = fixture();
  let approveCalls = 0;
  f.context.api = async (url) => {
    if (url.includes('/approve')) { approveCalls++; return { json: async () => ({ status: 'approved' }) }; }
    if (url.startsWith('/api/approvals')) return { json: async () => [] };
    return { json: async () => ({}) };
  };
  f.context.loadApprovals = async () => {};
  f.context.loadStatus = async () => {};
  const approval = {
    id: 'appr2', tool: 'forget', category: 'delete', summary: 'Forget fact 1',
    details: {}, args_hash: 'b'.repeat(64), needs_confirm: true, status: 'pending',
    args: { fact_id: 1 }, tainted: false,
  };
  let clock = 1_000_000;
  f.context.Date = { now: () => clock };
  const card = f.run('renderApprovalCard(' + JSON.stringify(approval) + ')');
  const approveBtn = card.children.find((c) => c.className === 'actions').children[0];
  await approveBtn.onclick();
  assert.equal(approveCalls, 0);
  assert.match(approveBtn.textContent, /Tap again/);
  clock += 1200;
  await approveBtn.onclick();
  await new Promise((r) => setImmediate(r));
  assert.equal(approveCalls, 1);
  assert.equal(approveBtn.textContent, 'Approved');
});

test('a double-click does not count as the confirming second tap', async () => {
  // Seen on Contabo: the second tap is meant to be a separate decision.
  const f = fixture();
  let approveCalls = 0;
  f.context.api = async (url) => {
    if (url.includes('/approve')) { approveCalls++; return { json: async () => ({ status: 'approved' }) }; }
    return { json: async () => [] };
  };
  f.context.loadApprovals = async () => {};
  f.context.loadStatus = async () => {};
  let clock = 1_000_000;
  f.context.Date = { now: () => clock };
  const approval = {
    id: 'appr3', tool: 'browser_click', category: 'payment', summary: 'Click “Submit order”',
    details: {}, args_hash: 'c'.repeat(64), needs_confirm: true, status: 'pending', args: { ref: 'e13' }, tainted: true,
  };
  const card = f.run('renderApprovalCard(' + JSON.stringify(approval) + ')');
  const approveBtn = card.children.find((c) => c.className === 'actions').children[0];
  await approveBtn.onclick();
  clock += 150;  // the second click of a double-click
  await approveBtn.onclick();
  clock += 600;
  await approveBtn.onclick();
  await new Promise((r) => setImmediate(r));
  assert.equal(approveCalls, 0);
  assert.match(approveBtn.textContent, /Tap again/);
  assert.equal(approveBtn.disabled, false);
  clock += 400;  // a full second after the first tap
  await approveBtn.onclick();
  await new Promise((r) => setImmediate(r));
  assert.equal(approveCalls, 1);
});

test('a failed confirmed approval starts over at the first tap', async () => {
  const f = fixture();
  let approveCalls = 0;
  f.context.api = async (url) => {
    if (url.includes('/approve')) { approveCalls++; throw new Error('expired'); }
    return { json: async () => [] };
  };
  f.context.loadApprovals = async () => {};
  f.context.loadStatus = async () => {};
  let clock = 1_000_000;
  f.context.Date = { now: () => clock };
  const approval = {
    id: 'appr4', tool: 'forget', category: 'delete', summary: 'Forget fact 2',
    details: {}, args_hash: 'd'.repeat(64), needs_confirm: true, status: 'pending', args: { fact_id: 2 }, tainted: false,
  };
  const card = f.run('renderApprovalCard(' + JSON.stringify(approval) + ')');
  const approveBtn = card.children.find((c) => c.className === 'actions').children[0];
  await approveBtn.onclick();
  clock += 1500;
  await approveBtn.onclick();
  await new Promise((r) => setImmediate(r));
  assert.equal(approveCalls, 1);
  assert.equal(approveBtn.textContent, 'Approve');
  clock += 1500;
  await approveBtn.onclick();  // only arms again
  assert.equal(approveCalls, 1);
  assert.match(approveBtn.textContent, /Tap again/);
});

test('composer is locked while an approval is pending', async () => {
  const f = fixture();
  f.run('setComposerLocked(true)');
  assert.equal(f.get('composer').classes.has('locked'), true);
  assert.equal(f.get('send').disabled, true);
  assert.equal(f.get('input').disabled, true);
  f.get('input').value = 'yes approve';
  f.get('composer').onsubmit({ preventDefault() {} });
  assert.equal(f.run('composerLocked'), true);
  f.run('setComposerLocked(false)');
  assert.equal(f.get('composer').classes.has('locked'), false);
});


test('stop unlocks composer even if approval_resolved never arrives', async () => {
  const f = fixture();
  f.run('currentChat = 1');
  f.run('setComposerLocked(true)');
  f.run('setSending(true)');
  let stopped = false;
  f.context.api = async (url) => {
    if (url.endsWith('/stop')) { stopped = true; return { json: async () => ({ ok: true, stopped: true }) }; }
    if (url === '/api/status') return { json: async () => ({ csrf: 'x', name: 'T', model: 'm', calls_left: 1, daily_limit: 1, pending_approvals: 0 }) };
    return { json: async () => ({}) };
  };
  f.context.loadStatus = async () => {};
  await f.get('stop').onclick();
  await new Promise((r) => setImmediate(r));
  assert.equal(stopped, true);
  assert.equal(f.run('composerLocked'), false);
  assert.equal(f.get('composer').classes.has('locked'), false);
  assert.equal(f.get('input').disabled, false);
});

test('send finally unlocks composer after stream ends without approval_resolved', async () => {
  const f = fixture();
  f.run('currentChat = 3');
  f.context.loadStatus = async () => {};
  // Stream locks on approval_required, then ends without approval_resolved.
  f.context.api = async (url) => {
    if (url.endsWith('/send')) {
      return sseStream([
        { type: 'approval_required', approval: {
          id: 'a1', category: 'delete', summary: 'x', args_hash: 'c'.repeat(64),
          status: 'pending', needs_confirm: false, details: {}, args: {}, tainted: false,
        } },
        { type: 'done', reply: 'gave up' },
      ]);
    }
    if (url.endsWith('/messages')) return { json: async () => ({ messages: [] }) };
    return { json: async () => ({}) };
  };
  await f.run('send("do it")');
  assert.equal(f.run('composerLocked'), false);
  assert.equal(f.get('composer').classes.has('locked'), false);
});
