const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// A small DOM fixture runs the actual shipped script without packages or network access.
class Element {
  constructor() {
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
  let nextTimer = 1;
  const context = vm.createContext({
    document: { getElementById: get, createElement: () => new Element() },
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
  return { context, get, run: (code) => vm.runInContext(code, context) };
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const finishedStream = { body: { getReader: () => ({ read: async () => ({ done: true }) }) } };

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
  assert.deepEqual(calls, ['/api/chats', '/api/chats/7/send']);
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
