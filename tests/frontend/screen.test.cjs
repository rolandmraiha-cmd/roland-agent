const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// The shipped screen page script, run against a small DOM and a stand-in for noVNC's RFB.
const SOURCE = fs.readFileSync(path.join(__dirname, '../../agent/web/static/screen.js'), 'utf8');
const NOVNC = 'import("/screen/novnc/core/rfb.js")';
const PASSWORD = 'Fu11pw9Z';

class Element {
  constructor() {
    this.children = []; this.textContent = ''; this.value = ''; this.hidden = false; this.disabled = false;
    this.listeners = {};
  }
  replaceChildren(...nodes) { this.children = nodes; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  focus() { this.focused = true; }
}

class FakeRFB {
  constructor(target, url, options) {
    this.target = target; this.url = url; this.options = options;
    this.listeners = {}; this.keys = []; this.disconnected = false;
    FakeRFB.made.push(this);
  }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  sendKey(sym, code) { this.keys.push([sym, code]); }
  disconnect() { this.disconnected = true; }
}

async function fixture(search, { fail = {}, narrow = null } = {}) {
  assert.equal(SOURCE.split(NOVNC).length, 2, 'noVNC is imported once, from this site only');
  FakeRFB.made = [];
  const elements = new Map();
  const get = (id) => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  const calls = [];
  const timers = [];
  const pageEvents = {};
  const location = { search, protocol: 'https:', host: 'agent.test', href: 'https://agent.test/screen' + search };
  const context = vm.createContext({
    document: { getElementById: get },
    window: { addEventListener: (name, fn) => { pageEvents[name] = fn; }, close() { context.closed = true; } },
    location, URLSearchParams, encodeURIComponent, JSON,
    setInterval: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearInterval: (id) => { if (timers[id - 1]) timers[id - 1].cleared = true; },
    // `narrow` true or false gives the page a browser that can answer "is this a phone?".
    ...(narrow === null ? {} : { matchMedia: (query) => ({ matches: query === '(max-width: 700px)' && narrow }) }),
    __importRFB: async () => { if (fail.novnc) throw new Error('403'); return { default: FakeRFB }; },
    fetch: async (url, options = {}) => {
      const body = options.body ? JSON.parse(options.body) : undefined;
      calls.push({ url, method: options.method || 'GET', body, keepalive: options.keepalive, headers: options.headers });
      const refused = Object.keys(fail).find((part) => url.includes(part));
      if (refused) return { ok: false, status: 409, statusText: 'Conflict', json: async () => ({ detail: fail[refused] }) };
      if (url === '/api/status') return { ok: true, status: 200, json: async () => ({ csrf: 'csrf-token' }) };
      if (url === '/api/screen/session') {
        return { ok: true, status: 200, json: async () => ({
          id: `screen-${calls.filter((c) => c.url === url).length}`, mode: body.mode,
          ws_path: '/screen/websockify', vnc_password: PASSWORD, expires: 1,
        }) };
      }
      return { ok: true, status: 200, json: async () => ({ ok: true }) };
    },
  });
  vm.runInContext(SOURCE.replace(NOVNC, '__importRFB()'), context);
  const settle = async () => { for (let i = 0; i < 20; i += 1) await new Promise((r) => setImmediate(r)); };
  await settle();
  const posts = (part) => calls.filter((c) => c.url.includes(part));
  return { context, get, calls, posts, timers, pageEvents, location, settle, rfb: () => FakeRFB.made[FakeRFB.made.length - 1] };
}
const typeInto = (f, value) => { f.get('screen-typing').value = value; f.get('screen-typing').listeners.input(); };
const key = (f, name, extra = {}) => {
  const event = { key: name, prevented: false, preventDefault() { this.prevented = true; }, ...extra };
  f.get('screen-typing').listeners.keydown(event);
  return event;
};

test('watching uses the view-only side of noVNC and never keeps the password anywhere', async () => {
  const f = await fixture('?mode=watch');
  assert.deepEqual(f.calls.map((c) => [c.method, c.url]), [['GET', '/api/status'], ['POST', '/api/screen/session']]);
  assert.deepEqual(f.posts('/api/screen/session')[0].body, { mode: 'watch' });
  assert.equal(f.posts('/api/screen/session')[0].headers['X-CSRF-Token'], 'csrf-token');
  const rfb = f.rfb();
  assert.equal(rfb.url, 'wss://agent.test/screen/websockify');
  assert.equal(JSON.stringify(rfb.options), JSON.stringify({ credentials: { password: PASSWORD } }));
  assert.equal(rfb.viewOnly, true);
  assert.equal(rfb.scaleViewport, true);
  assert.equal(rfb.target, f.get('screen'));
  // Not in the page, not in the address, not in what the script keeps.
  for (const id of ['screen-status', 'screen-error', 'screen-mode', 'screen-typing']) {
    assert.equal(String(f.get(id).textContent + f.get(id).value).includes(PASSWORD), false);
  }
  assert.equal(f.location.href.includes(PASSWORD), false);
  assert.equal(vm.runInContext('JSON.stringify(session)', f.context), '{"id":"screen-1"}');
  for (const banned of ['console.', 'localStorage', 'sessionStorage', 'innerHTML', 'document.cookie', 'location.hash']) {
    assert.equal(SOURCE.includes(banned), false, banned);
  }
  // Watching shows no control buttons, and offers to take control.
  assert.equal(f.get('screen-keyboard').hidden, true);
  assert.equal(f.get('screen-handback').hidden, true);
  assert.equal(f.get('screen-done').hidden, true);
  assert.equal(f.get('screen-control').hidden, false);
  assert.equal(f.get('screen-mode').textContent, 'Watching the agent');
  // Typing while watching goes nowhere.
  typeInto(f, 'secret');
  key(f, 'Enter');
  assert.deepEqual(rfb.keys, []);
});

test('an unknown mode means watch, never control', async () => {
  for (const search of ['', '?mode=admin', '?mode=CONTROL', '?signin=sig-1']) {
    const f = await fixture(search);
    assert.equal(f.posts('/api/screen/session')[0].body.mode, 'watch', search);
    assert.equal(f.posts('/api/screen/session')[0].body.signin_id, undefined, search);
    assert.equal(f.rfb().viewOnly, true);
  }
});

test('the sign-in screen takes control, and I\'m done is the button that ends it', async () => {
  const f = await fixture('?mode=control&signin=sig%2F1');
  assert.deepEqual(f.posts('/api/screen/session')[0].body, { mode: 'control', signin_id: 'sig/1' });
  const rfb = f.rfb();
  assert.equal(rfb.viewOnly, false);
  assert.equal(f.get('screen-done').hidden, false);
  assert.equal(f.get('screen-keyboard').hidden, false);
  assert.equal(f.get('screen-handback').hidden, false);
  assert.equal(f.get('screen-watch').hidden, false);
  assert.equal(f.get('screen-mode').textContent, 'You have the browser');
  rfb.listeners.connect();
  assert.equal(f.get('screen-status').textContent, 'Connected. The agent is paused.');
  await f.get('screen-done').onclick();
  assert.deepEqual(f.posts('/api/signin/').map((c) => [c.method, c.url]), [['POST', '/api/signin/sig%2F1/done']]);
  assert.equal(rfb.disconnected, true);
  assert.equal(f.get('screen-done').hidden, true);
  assert.equal(f.get('screen-keyboard').hidden, true);
  assert.match(f.get('screen-status').textContent, /The agent has the browser again/);
  // Nothing can be started again from a finished page.
  await f.get('screen-control').onclick();
  assert.equal(f.posts('/api/screen/session').length, 1);
});

test('a sign-in that is already over is reported, and its Done button goes away', async () => {
  const f = await fixture('?mode=control&signin=sig-1', { fail: { '/api/signin/': 'that sign-in is no longer waiting' } });
  await f.get('screen-done').onclick();
  assert.equal(f.get('screen-error').textContent, 'that sign-in is no longer waiting');
  assert.equal(f.get('screen-error').hidden, false);
  assert.equal(f.get('screen-done').hidden, true);
  assert.equal(f.rfb().disconnected, false);  // he still has the control he asked for
});

test('typing from a phone is passed on key by key and nothing stays in the box', async () => {
  const f = await fixture('?mode=control');
  const rfb = f.rfb();
  const box = f.get('screen-typing');
  f.get('screen-keyboard').onclick();
  assert.equal(box.focused, true);
  typeInto(f, 'a');
  typeInto(f, 'ab');
  assert.deepEqual(rfb.keys, [[0x61, null], [0x62, null]]);
  // A predictive keyboard rewrites the word: take back what changed, then type it again.
  typeInto(f, 'aX');
  assert.deepEqual(rfb.keys.slice(2), [[0xff08, null], [0x58, null]]);
  typeInto(f, 'a');
  assert.deepEqual(rfb.keys.slice(4), [[0xff08, null]]);
  // A space ends the word and empties the box.
  typeInto(f, 'a ');
  assert.deepEqual(rfb.keys.slice(5), [[0x20, null]]);
  assert.equal(box.value, '');
  // Finnish letters, a euro sign and an emoji.
  typeInto(f, 'äÖå€😀');
  assert.deepEqual(rfb.keys.slice(6).map(([sym]) => sym), [0xe4, 0xd6, 0xe5, 0x010020ac, 0x0101f600]);
  rfb.keys.length = 0;
  box.listeners.blur();
  assert.equal(box.value, '');
  // Keys that put no text in the box.
  for (const [name, sym] of [['Enter', 0xff0d], ['Tab', 0xff09], ['Escape', 0xff1b], ['ArrowLeft', 0xff51], ['Backspace', 0xff08]]) {
    const event = key(f, name);
    assert.equal(event.prevented, true, name);
    assert.deepEqual(rfb.keys.pop(), [sym, null], name);
  }
  // Backspace with text in the box is the box's own business (the input event passes it on),
  // an unknown key is ignored, and a key still being composed is left alone.
  box.value = 'x';
  assert.equal(key(f, 'Backspace').prevented, false);
  assert.equal(key(f, 'a').prevented, false);
  assert.equal(key(f, 'Enter', { isComposing: true }).prevented, false);
  assert.equal(key(f, 'Enter', { keyCode: 229 }).prevented, false);
  assert.deepEqual(rfb.keys, []);
  // A very long run of text without a space is flushed too.
  box.value = '';
  box.listeners.blur();
  typeInto(f, 'z'.repeat(201));
  assert.equal(box.value, '');
  assert.equal(rfb.keys.length, 201);
});

test('the heartbeat keeps the session, and a session that is over closes the screen', async () => {
  const f = await fixture('?mode=control');
  const beat = f.timers.find((t) => t.ms === 60000 && !t.cleared);
  assert.ok(beat);
  await beat.fn();
  assert.deepEqual(f.posts('/api/screen/heartbeat').map((c) => c.body), [{ id: 'screen-1' }]);
  assert.equal(f.rfb().disconnected, false);
  const over = await fixture('?mode=control', { fail: { '/api/screen/heartbeat': 'that screen session is over' } });
  await over.timers.find((t) => t.ms === 60000).fn();
  assert.equal(over.rfb().disconnected, true);
  assert.equal(over.get('screen-status').textContent, 'This screen session is over.');
  assert.equal(over.get('screen-keyboard').hidden, true);
  assert.equal(over.timers.find((t) => t.ms === 60000).cleared, true);
});

test('handing back, switching and leaving all give the browser back at once', async () => {
  const f = await fixture('?mode=control');
  const first = f.rfb();
  await f.get('screen-handback').onclick();
  assert.deepEqual(f.posts('/api/screen/release').map((c) => c.body), [{ id: 'screen-1' }]);
  assert.equal(first.disconnected, true);
  assert.equal(f.get('screen-status').textContent, 'The agent has the browser again.');
  assert.equal(f.get('screen-keyboard').hidden, true);

  const swap = await fixture('?mode=control');
  await swap.get('screen-watch').onclick();
  await swap.settle();
  assert.deepEqual(swap.posts('/api/screen/release').map((c) => c.body), [{ id: 'screen-1' }]);
  assert.deepEqual(swap.posts('/api/screen/session').map((c) => c.body.mode), ['control', 'watch']);
  assert.equal(swap.rfb().viewOnly, true);

  const leave = await fixture('?mode=control');
  leave.pageEvents.pagehide();
  await leave.settle();
  assert.deepEqual(leave.posts('/api/screen/release').map((c) => [c.body, c.keepalive]), [[{ id: 'screen-1' }, true]]);

  const close = await fixture('?mode=watch');
  await close.get('screen-close').onclick();
  assert.equal(close.posts('/api/screen/release').length, 1);
  assert.equal(close.location.href, '/');

  // The screen dropping on its own (network, or core cutting it) releases the session too.
  const dropped = await fixture('?mode=control');
  dropped.rfb().listeners.disconnect();
  await dropped.settle();
  assert.deepEqual(dropped.posts('/api/screen/release').map((c) => c.body), [{ id: 'screen-1' }]);
  assert.equal(dropped.get('screen-status').textContent, 'The screen was disconnected.');
  // noVNC said it is closed: it is not told to close again (it would log an error), not
  // even when the page is closed afterwards.
  assert.equal(dropped.rfb().disconnected, false);
  await dropped.get('screen-close').onclick();
  assert.equal(dropped.rfb().disconnected, false);
});

test('Zoom in shows the screen full size with dragging; Fit screen scales it back', async () => {
  const f = await fixture('?mode=control');
  const rfb = f.rfb();
  assert.deepEqual([rfb.scaleViewport, rfb.clipViewport, rfb.dragViewport], [true, false, false]);
  assert.equal(f.get('screen-zoom').hidden, false);
  assert.equal(f.get('screen-zoom').textContent, 'Zoom in');
  f.get('screen-zoom').onclick();
  assert.deepEqual([rfb.scaleViewport, rfb.clipViewport, rfb.dragViewport], [false, true, true]);
  assert.equal(f.get('screen-zoom').textContent, 'Fit screen');
  // The choice carries over when the mode changes.
  await f.get('screen-watch').onclick();
  await f.settle();
  assert.deepEqual([f.rfb().scaleViewport, f.rfb().clipViewport, f.rfb().dragViewport], [false, true, true]);
  f.get('screen-zoom').onclick();
  assert.deepEqual([f.rfb().scaleViewport, f.rfb().clipViewport, f.rfb().dragViewport], [true, false, false]);
});

test('a phone starts zoomed in, a wide window starts fitted', async () => {
  const phone = await fixture('?mode=control', { narrow: true });
  assert.deepEqual([phone.rfb().scaleViewport, phone.rfb().clipViewport, phone.rfb().dragViewport], [false, true, true]);
  assert.equal(phone.get('screen-zoom').textContent, 'Fit screen');
  phone.get('screen-zoom').onclick();
  assert.deepEqual([phone.rfb().scaleViewport, phone.rfb().clipViewport, phone.rfb().dragViewport], [true, false, false]);
  const wide = await fixture('?mode=control', { narrow: false });
  assert.deepEqual([wide.rfb().scaleViewport, wide.rfb().clipViewport, wide.rfb().dragViewport], [true, false, false]);
  assert.equal(wide.get('screen-zoom').textContent, 'Zoom in');
});

test('a refused session or missing noVNC shows an error and connects nothing', async () => {
  const refused = await fixture('?mode=control', { fail: { '/api/screen/session': "the browser isn't reachable right now" } });
  assert.equal(FakeRFB.made.length, 0);
  assert.equal(refused.get('screen-error').textContent, "the browser isn't reachable right now");
  assert.equal(refused.get('screen-status').textContent, 'Not connected.');
  assert.equal(refused.get('screen-keyboard').hidden, true);

  const blocked = await fixture('?mode=watch', { fail: { novnc: true } });
  assert.equal(FakeRFB.made.length, 0);
  assert.equal(blocked.get('screen-error').textContent, 'The screen could not be opened.');
  // The session it had opened is given back rather than left to time out.
  assert.deepEqual(blocked.posts('/api/screen/release').map((c) => c.body), [{ id: 'screen-1' }]);
});

test('the screen page loads its script from this site and holds no password or inline code', () => {
  const html = fs.readFileSync(path.join(__dirname, '../../agent/web/static/screen.html'), 'utf8');
  assert.match(html, /<script src="\/static\/screen\.js"><\/script>/);
  assert.equal((html.match(/<script/g) || []).length, 1);
  assert.equal(/ on[a-z]+=/.test(html), false);
  assert.equal(/password|passwd/i.test(html), false);
  assert.match(html, /<textarea id="screen-typing"[^>]*autocomplete="off"[^>]*spellcheck="false"/);
});
