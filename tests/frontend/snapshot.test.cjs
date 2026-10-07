// The browser service's page script (browserd/snapshot.js), run here against a small stand-in
// for the page: which fields count as secret, that a secret field's value is never read, and
// that a form field's name can't hide the form's real method and address.
// The same script against a real Chromium: tests/integration/test_browser_live.py.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', '..', 'browserd', 'snapshot.js'), 'utf8');

class Node {
  get parentElement() { return this._parent || null; }
  get textContent() { return this._text || ''; }
  get baseURI() { return 'https://shop.example/cart'; }
  getRootNode() { return { getElementById: () => null }; }
}
class Element extends Node {
  constructor(tag, attrs = {}, more = {}) {
    super();
    this._tag = tag; this._attrs = attrs; this._kids = []; this._text = more.text || '';
    this._disabled = more.disabled === true;
  }
  get tagName() { return this._tag.toUpperCase(); }
  get nodeType() { return 1; }
  get lastChild() { return this._text ? { nodeType: 3, nodeValue: this._text, previousSibling: null } : null; }
  get children() { return this._kids; }
  get shadowRoot() { return null; }
  get firstElementChild() { return this._kids[0] || null; }
  getAttribute(name) { return name in this._attrs ? this._attrs[name] : null; }
  hasAttribute(name) { return name in this._attrs; }
  setAttribute(name, value) { this._attrs[name] = String(value); }
  matches(selector) { return selector === ':disabled' ? this._disabled : false; }
  closest(selector) {
    if (selector !== 'form') return null;
    for (let node = this; node; node = node._parent) if (node._tag === 'form') return node;
    return null;
  }
  querySelector(selector) {
    if (!selector.startsWith('button')) return null;
    return this._kids.find((kid) => kid._tag === 'button') || null;
  }
  getBoundingClientRect() { return { width: 10, height: 10 }; }
  append(...kids) { for (const kid of kids) { kid._parent = this; this._kids.push(kid); } return this; }
}
class HTMLElement extends Element {
  get innerText() { return this._text || ''; }
  get isContentEditable() { return this._attrs.contenteditable === 'true'; }
}
class HTMLFormElement extends HTMLElement {}
class Document {
  get body() { return null; }
  get documentElement() { return null; }
  get activeElement() { return null; }
  hasFocus() { return false; }
}
Object.defineProperty(Document.prototype, 'baseURI', { get() { return 'https://shop.example/cart'; } });

const context = { Node, Element, HTMLElement, HTMLFormElement, Document, document: new Document(), URL };
const script = vm.runInNewContext(`(${source})`, context);

// A field whose value counts how often it was read.
function field(attrs, value, more = {}) {
  const input = new HTMLElement(more.tag || 'input', attrs, more);
  input.reads = 0;
  Object.defineProperty(input, 'value', { get() { input.reads += 1; return value; } });
  input.labels = (more.label ? [new HTMLElement('label', {}, { text: more.label })] : []);
  return input;
}
const describe = (element, sensitive = 'word') => script(element, { op: 'describe', sensitive });

test('helpers: which field names mean a secret (word mode, the default)', () => {
  const { secretName } = script({ op: 'helpers' });
  const secret = ['password', 'Password', 'user_password', 'passwd', 'pwd', 'loginPwd', 'new-password', 'salasana',
    'Salasana', 'tunnusluku', 'pin', 'PIN', 'pin_code', 'pinCode', 'userPin', 'card-pin', 'otp', 'OTP', 'otp-code',
    'smsOtp', 'sms_code', 'totp', '2fa', '2fa_token', 'cvc', 'CVV', 'cvv2', 'cardnumber', 'card-number',
    'card_number', 'cardNumber', 'Card number', 'iban', 'IBAN', 'security code', 'securityCode',
    'verification_code', 'one-time code', 'pass', 'user pass', 'mfa', 'csc', 'korttinumero', 'turvakoodi',
    'vahvistuskoodi', 'Kortin numero', 'passphrase', 'passcode', 'authCode', 'loginCode'];
  const plain = ['shipping', 'shipping_address', 'passenger', 'passengers', 'compass', 'bypass_cache', 'opinion',
    'spinner', 'pinterest', 'pineapple', 'mapping', 'option', 'hotpot', 'bishop', 'search', 'q', 'email', 'name',
    'first_name', 'city', 'postcode', 'zip', 'coupon', 'coupon_code', 'promo code', 'country code', 'message',
    'comment', 'subject', 'username', 'phone', 'quantity', 'cardholder', 'card_holder_name', 'time', 'one',
    'code', 'number', 'house number', 'osoite', 'nimi', 'viesti', '', 'passport_country'];
  for (const name of secret) assert.equal(secretName(name, 'word'), true, name);
  for (const name of plain) assert.equal(secretName(name, 'word'), false, name);
});

test('helpers: the spec\'s literal rule (substring mode) is stricter and catches more', () => {
  const { secretName } = script({ op: 'helpers' });
  for (const name of ['password', 'pwd', 'pin', 'otp', '2fa', 'totp', 'cvc', 'cvv', 'cardnumber', 'card-number',
    'iban', 'passwd', 'shipping', 'passenger', 'compass', 'opinion', 'spinner', 'mapping']) {
    assert.equal(secretName(name, 'substring'), true, name);
  }
  for (const name of ['search', 'email', 'city', 'message', 'coupon', 'name', '']) {
    assert.equal(secretName(name, 'substring'), false, name);
  }
  assert.equal(secretName(`${'x'.repeat(300)}password`, 'substring'), false); // only the first 200 characters count
});

test('helpers: names are split into words the same way everywhere', () => {
  const { nameWords, tidy } = script({ op: 'helpers' });
  assert.deepEqual(Array.from(nameWords('userPinCode')), ['user', 'pin', 'code']);
  assert.deepEqual(Array.from(nameWords('card_number-2')), ['card', 'number', '2']);
  assert.deepEqual(Array.from(nameWords('otp1')), ['otp', '1']);
  assert.deepEqual(Array.from(nameWords('  Sähköposti (työ)  ')), ['sähköposti', 'työ']);
  assert.equal(tidy('  a \n\t b   c ', 20), 'a b c');
  assert.equal(tidy('x'.repeat(500), 200).length, 200);
  assert.equal(tidy(`half \ud83d pair`, 50), 'half � pair');
  assert.equal(tidy(42, 10), '');
  assert.equal(tidy(null, 10), '');
});

test('the value of a password field is never read', () => {
  const input = field({ type: 'password', name: 'pw', value: 'hunter2' }, 'hunter2');
  const out = describe(input);
  assert.equal(out.sensitive, true);
  assert.equal(out.value, '');
  assert.equal(input.reads, 0);
  assert.ok(!JSON.stringify(out).includes('hunter2'));
});

test('the value of a field that is secret by its name, label or hint is never read', () => {
  const cases = [
    field({ type: 'text', name: 'pin' }, '1234'),
    field({ type: 'tel', id: 'otp-code' }, '987654'),
    field({ type: 'text', 'aria-label': 'Card number' }, '4111'),
    field({ type: 'text', name: 'x', autocomplete: 'cc-csc' }, '321'),
    field({ type: 'text', name: 'x', autocomplete: 'section-pay One-Time-Code' }, '555'),
    field({ type: 'text', name: 'f1', placeholder: 'Security code' }, '321'),
    field({ type: 'text', name: 'f2' }, '1234', { label: 'PIN' }),
    field({ name: 'recovery' }, 'words', { tag: 'textarea', label: 'Passphrase' }),
  ];
  for (const input of cases) {
    const out = describe(input);
    assert.equal(out.sensitive, true, JSON.stringify(input._attrs));
    assert.equal(out.value, '');
    assert.equal(input.reads, 0, JSON.stringify(input._attrs));
  }
});

test('ordinary fields are read, cut to 200 characters, and similar names are left alone', () => {
  const city = field({ type: 'text', name: 'shipping_city' }, 'Helsinki', { label: 'City' });
  const out = describe(city);
  assert.equal(out.sensitive, false);
  assert.equal(out.value, 'Helsinki');
  assert.equal(out.name, 'City');
  assert.equal(city.reads, 1);
  assert.equal(describe(field({ type: 'text', name: 'note' }, 'x'.repeat(900))).value.length, 200);
  // The spec's literal rule calls "shipping" secret (it contains "pin"): the value stays unread.
  const strict = field({ type: 'text', name: 'shipping_city' }, 'Helsinki');
  assert.equal(describe(strict, 'substring').sensitive, true);
  assert.equal(strict.reads, 0);
  // Tick boxes and file fields have no value worth showing.
  const box = field({ type: 'checkbox', name: 'news' }, 'on');
  assert.equal(describe(box).value, '');
  assert.equal(box.reads, 0);
});

test('a field name cannot hide where its form really goes', () => {
  const form = new HTMLFormElement('form', { action: '/order', method: 'POST' });
  const button = new HTMLElement('button', { type: 'submit' }, { text: 'Book now' });
  const children = field({ type: 'text', name: 'children' }, '1');
  form.append(children, button);
  // What <input name="..."> does to a real form object: the property is the field.
  for (const name of ['action', 'method', 'children', 'tagName', 'getAttribute', 'querySelector', 'matches',
    'closest', 'textContent', 'id']) {
    Object.defineProperty(form, name, { value: field({ name }, 'x') });
  }
  const out = describe(button);
  assert.equal(out.in_form, true);
  assert.equal(out.form_method, 'post');
  assert.equal(out.form_action, 'https://shop.example/order');
  assert.equal(out.submits, true);
  assert.equal(out.name, 'Book now');
  const inside = describe(children);
  assert.equal(inside.form_method, 'post');
  assert.equal(inside.form_submit_name, 'Book now');
});

test('a submit button can override the form, and odd methods count as GET', () => {
  const form = new HTMLFormElement('form', { action: '/search', method: 'get' });
  const pay = new HTMLElement('button', { formaction: 'https://pay.example/now', formmethod: 'POST' }, { text: 'Pay' });
  const plain = new HTMLElement('button', { type: 'button' }, { text: 'More' });
  form.append(pay, plain);
  assert.deepEqual([describe(pay).form_method, describe(pay).form_action], ['post', 'https://pay.example/now']);
  assert.deepEqual([describe(plain).form_method, describe(plain).form_action, describe(plain).submits],
    ['get', 'https://shop.example/search', false]);
  const odd = new HTMLFormElement('form', { method: 'delete' });
  const go = new HTMLElement('button', {}, { text: 'Go' });
  odd.append(go);
  assert.equal(describe(go).form_method, 'get');
  assert.equal(describe(go).form_action, 'https://shop.example/cart'); // no action: the page itself
});

test('facts that the classifier reads are reported as plain values', () => {
  const link = new HTMLElement('a', { href: '/p/blue-mug', title: ' Blue   mug ', 'aria-expanded': 'TRUE',
    'aria-haspopup': 'menu', role: 'Button link', 'data-ra-ref': 'e12' }, { text: 'Blue mug' });
  const out = describe(link);
  assert.deepEqual(
    [out.ref, out.tag, out.role, out.name, out.href, out.title_attr, out.aria_expanded, out.aria_haspopup, out.in_form],
    ['e12', 'a', 'button', 'Blue mug', 'https://shop.example/p/blue-mug', 'Blue mug', true, true, false],
  );
  const forged = new HTMLElement('a', { href: 'javascript:alert(1)', 'data-ra-ref': 'e1"] *' }, { text: 'x' });
  assert.equal(describe(forged).ref, '');
  assert.equal(describe(new HTMLElement('button', { 'aria-disabled': 'true' }, { text: 'Off' })).disabled, true);
  assert.equal(describe(new HTMLElement('button', {}, { text: 'Off', disabled: true })).disabled, true);
});

test('only the three fixed jobs exist', () => {
  assert.deepEqual({ ...script({ op: 'run', code: 'alert(1)' }) }, { error: 'bad_op' });
  assert.deepEqual({ ...script({}) }, { error: 'bad_op' });
  assert.deepEqual({ ...script({ op: 'describe' }) }, { error: 'no_such_element' });
  assert.deepEqual({ ...script({ op: 'focused' }) }, { error: 'no_focused_element' });
});
