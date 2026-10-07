// browserd page script (§6.5). A fixed, reviewed file that ships in the browser image.
// It is never built from agent or page input: browserd passes it to Playwright as it is.
//
// It only reads the page. The one thing it writes is a data-ra-ref="eN" label on links,
// buttons and fields, so that a later action can name the element it means.
// The value of a password, code or card field is never read, not even here.
//
// Called as a function. From page.evaluate: (args). From locator.evaluate: (element, args).
(first, second) => {
  "use strict";
  const element = first instanceof Element ? first : null;
  const args = (element ? second : first) || {};

  const MAX_NAME = 200;
  const MAX_VALUE = 200;
  const MAX_URL = 2048;
  const MAX_VISITED = 20000; // elements looked at in one snapshot
  const MAX_STRUCTURE = 80; // headings and page regions listed in one snapshot
  const MAX_OPTIONS = 20;

  const INTERACTIVE = [
    "a[href]", "button", "input", "select", "textarea", "summary", "area[href]",
    '[role="button"]', '[role="link"]', '[role="tab"]', '[role="menuitem"]', '[role="checkbox"]',
    '[role="radio"]', '[role="switch"]', '[role="option"]', '[role="combobox"]',
    '[role="textbox"]', '[role="searchbox"]', "[onclick]",
  ].join(",");
  const REGIONS = { MAIN: "main", NAV: "navigation", HEADER: "banner", FOOTER: "contentinfo",
    ASIDE: "complementary", DIALOG: "dialog", FORM: "form", SEARCH: "search" };
  const REGION_ROLES = ["main", "navigation", "banner", "contentinfo", "complementary", "dialog",
    "alertdialog", "form", "search"];

  // --- text helpers ---
  const wellFormed = (s) => (typeof s.toWellFormed === "function" ? s.toWellFormed() : s.replace(/\p{Cs}/gu, "�"));
  const tidy = (value, max) => {
    if (typeof value !== "string") return "";
    return wellFormed(value.slice(0, max * 8).replace(/\s+/g, " ").trim().slice(0, max));
  };
  const attr = (el, name) => {
    const value = el.getAttribute(name);
    return typeof value === "string" ? value : "";
  };
  const lower = (el, name) => attr(el, name).trim().toLowerCase();
  const absolute = (raw) => {
    try {
      return wellFormed(new URL(raw, document.baseURI).href.slice(0, MAX_URL));
    } catch (error) {
      return "";
    }
  };

  // --- which fields hold secrets ---
  // Always: type=password and the autocomplete hints for passwords, one-time codes and cards.
  // By name: two ways of matching, chosen by browserd (BROWSER_SENSITIVE_MATCH).
  const SECRET_AUTOCOMPLETE = ["current-password", "new-password", "one-time-code", "cc-number",
    "cc-csc", "cc-exp", "cc-exp-month", "cc-exp-year"];
  // "substring": the spec's literal list, matched anywhere in name, id or aria-label.
  const SPEC_WORDS = ["pass", "pwd", "passwd", "otp", "2fa", "totp", "cvc", "cvv", "cardnumber",
    "card-number", "iban", "pin"];
  // "word": long, unmistakable words match anywhere...
  const ANYWHERE = ["password", "passwd", "passcode", "passphrase", "pwd", "salasana", "tunnusluku",
    "cardnumber", "card-number", "card_number", "ccnum", "korttinumero", "cvc", "cvv", "totp",
    "2fa", "iban", "pswd", "securitycode", "turvakoodi", "vahvistuskoodi", "onetimecode"];
  // ...short ones only as a whole word, so "shipping" and "passenger" are left alone, or
  // glued to a common first part such as "userpin" or "smsotp"...
  const SHORT_WORDS = ["pass", "pw", "psw", "pin", "otp", "mfa", "cvn", "csc"];
  const FIRST_PARTS = ["user", "my", "new", "old", "card", "sms", "login", "bank", "atm", "sim",
    "debit", "credit", "email", "phone", "mobile", "confirm", "account", "current", "repeat"];
  const WHOLE_WORDS = new Set(SHORT_WORDS);
  for (const part of FIRST_PARTS) for (const word of ["pass", "pw", "pin", "otp"]) WHOLE_WORDS.add(part + word);
  for (const word of ["pin", "otp", "sms", "auth", "mfa"]) for (const tail of ["code", "koodi", "number"]) WHOLE_WORDS.add(word + tail);
  // ...and these pairs of neighbouring words.
  const WORD_PAIRS = [["security", "code"], ["verification", "code"], ["card", "number"],
    ["one", "time"], ["sms", "code"], ["auth", "code"], ["login", "code"], ["kortin", "numero"]];

  const nameWords = (text) => {
    const spaced = text.slice(0, 200).replace(/([\p{Ll}\p{N}])(\p{Lu})/gu, "$1 $2").toLowerCase();
    const out = [];
    for (const part of spaced.split(/[^\p{L}\p{N}]+/u)) {
      const pieces = part.match(/\p{L}+|\p{N}+/gu);
      if (pieces) out.push(...pieces);
    }
    return out;
  };
  const secretName = (text, mode) => {
    if (!text) return false;
    const plain = text.slice(0, 200).toLowerCase();
    if (mode === "substring") return SPEC_WORDS.some((word) => plain.includes(word));
    if (ANYWHERE.some((word) => plain.includes(word))) return true;
    const words = nameWords(text);
    if (words.some((word) => WHOLE_WORDS.has(word))) return true;
    for (let i = 0; i + 1 < words.length; i += 1) {
      if (WORD_PAIRS.some((pair) => pair[0] === words[i] && pair[1] === words[i + 1])) return true;
    }
    return false;
  };

  const TEXT_INPUTS = ["", "text", "password", "tel", "number", "email", "url", "search"];
  const takesText = (el) => {
    const tag = el.tagName;
    if (tag === "TEXTAREA") return true;
    if (tag === "INPUT") return TEXT_INPUTS.includes(lower(el, "type"));
    const role = lower(el, "role").split(" ")[0];
    return el.isContentEditable === true || role === "textbox" || role === "searchbox";
  };
  // The words of a <label>, without the text of a field it wraps (the options of a
  // <select>, the text inside a <textarea>): those are the field's contents, not its name.
  const NOT_LABEL_TEXT = ["SELECT", "TEXTAREA", "INPUT", "BUTTON", "SCRIPT", "STYLE", "TEMPLATE"];
  const wordsOf = (root) => {
    const parts = [];
    const stack = [root];
    let visited = 0;
    while (stack.length && visited < 300) {
      const node = stack.pop();
      visited += 1;
      if (node.nodeType === 3) parts.push(node.nodeValue || "");
      if (node.nodeType !== 1 || (node !== root && NOT_LABEL_TEXT.includes(node.tagName))) continue;
      for (let child = node.lastChild; child; child = child.previousSibling) stack.push(child);
    }
    return parts.join(" ");
  };
  const labelText = (el) => {
    try {
      if (el.labels && el.labels.length) {
        return tidy(Array.from(el.labels).slice(0, 3).map(wordsOf).join(" "), MAX_NAME);
      }
    } catch (error) { /* not a labelable element */ }
    return "";
  };
  const isSensitive = (el, mode) => {
    if (el.tagName === "INPUT" && lower(el, "type") === "password") return true;
    const hints = lower(el, "autocomplete").split(/\s+/);
    if (hints.some((hint) => SECRET_AUTOCOMPLETE.includes(hint))) return true;
    const named = [attr(el, "name"), attr(el, "id"), attr(el, "aria-label")];
    // The spec's rule looks at those three on any element. The word rule only looks at
    // things you can type into, and also reads their placeholder and label.
    if (mode === "substring") return named.some((text) => secretName(text, mode));
    if (!takesText(el)) return false;
    named.push(attr(el, "placeholder"), labelText(el));
    return named.some((text) => secretName(text, mode));
  };

  // Used by the tests under tests/frontend: nothing in a real page asks for this.
  if (args.op === "helpers") return { secretName, nameWords, tidy };

  const mode = args.sensitive === "substring" ? "substring" : "word";

  // --- what an element is ---
  const refOf = (el) => {
    const ref = attr(el, "data-ra-ref");
    return /^e[0-9]{1,6}$/.test(ref) ? ref : "";
  };
  const referenced = (el, name) => {
    const ids = attr(el, name).split(/\s+/).filter(Boolean).slice(0, 8);
    const root = el.getRootNode();
    const parts = [];
    for (const id of ids) {
      const target = root && root.getElementById ? root.getElementById(id) : null;
      if (target) parts.push(target.textContent || "");
    }
    return tidy(parts.join(" "), MAX_NAME);
  };
  const shownText = (el) => {
    let text = "";
    try { text = el.innerText || ""; } catch (error) { text = ""; }
    return tidy(text || el.textContent || "", MAX_NAME);
  };
  const accessibleName = (el) => {
    const tag = el.tagName;
    const kind = lower(el, "type");
    let name = tidy(attr(el, "aria-label"), MAX_NAME) || referenced(el, "aria-labelledby");
    if (name) return name;
    if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") {
      if (tag === "INPUT" && ["submit", "button", "reset"].includes(kind)) {
        name = tidy(attr(el, "value"), MAX_NAME) || (kind === "submit" ? "Submit" : kind === "reset" ? "Reset" : "");
      } else if (tag === "INPUT" && kind === "image") {
        name = tidy(attr(el, "alt"), MAX_NAME);
      }
      return name || labelText(el) || tidy(attr(el, "placeholder"), MAX_NAME)
        || tidy(attr(el, "title"), MAX_NAME) || tidy(attr(el, "name"), MAX_NAME);
    }
    // An editable area's own text is what was typed into it: that is its value, not its name.
    if (el.isContentEditable === true) {
      return tidy(attr(el, "title"), MAX_NAME) || tidy(attr(el, "aria-placeholder"), MAX_NAME)
        || tidy(attr(el, "data-placeholder"), MAX_NAME);
    }
    name = shownText(el);
    if (name) return name;
    const picture = el.querySelector("img[alt], svg[aria-label], [aria-label]");
    if (picture) name = tidy(attr(picture, "alt") || attr(picture, "aria-label"), MAX_NAME);
    return name || tidy(attr(el, "title"), MAX_NAME);
  };
  const formOf = (el) => {
    let form = null;
    try { form = el.form instanceof HTMLFormElement ? el.form : null; } catch (error) { form = null; }
    return form || el.closest("form");
  };
  const SUBMIT_CONTROLS = 'button:not([type]), button[type="submit" i], input[type="submit" i], input[type="image" i]';
  const isSubmitControl = (el) => {
    if (el.tagName === "BUTTON") return !["button", "reset"].includes(lower(el, "type"));
    return el.tagName === "INPUT" && ["submit", "image"].includes(lower(el, "type"));
  };
  const dialogTitle = (el) => {
    const box = el.closest('dialog, [role="dialog"], [role="alertdialog"]');
    if (!box) return "";
    const heading = box.querySelector("h1, h2, h3, [role='heading']");
    return (tidy(attr(box, "aria-label"), 80) || referenced(box, "aria-labelledby").slice(0, 80)
      || (heading ? tidy(heading.textContent || "", 80) : ""));
  };
  const isDisabled = (el) => {
    let off = false;
    try { off = el.matches(":disabled"); } catch (error) { off = false; }
    return off || lower(el, "aria-disabled") === "true";
  };

  const facts = (el) => {
    const tag = el.tagName.toLowerCase();
    const sensitive = isSensitive(el, mode);
    const form = formOf(el);
    const submit = form !== null && isSubmitControl(el);
    let method = "";
    let action = "";
    let submitName = "";
    if (form) {
      // Read attributes, not form.method / form.action: a field called "action" hides those.
      method = (submit && attr(el, "formmethod") ? lower(el, "formmethod") : lower(form, "method")) || "get";
      if (!["get", "post", "dialog"].includes(method)) method = "get";
      action = absolute(submit && el.hasAttribute("formaction") ? attr(el, "formaction") : attr(form, "action"));
      const button = submit ? el : form.querySelector(SUBMIT_CONTROLS);
      if (button && button !== el) submitName = accessibleName(button);
    }
    let value = "";
    if (!sensitive) {
      if (tag === "select") {
        value = tidy(Array.from(el.selectedOptions || [], (option) => option.textContent || "").join(", "), MAX_VALUE);
      } else if (tag === "input" || tag === "textarea") {
        const kind = lower(el, "type");
        value = ["checkbox", "radio", "file"].includes(kind) ? "" : tidy(String(el.value || ""), MAX_VALUE);
      } else if (tag === "button") {
        value = tidy(attr(el, "value"), MAX_VALUE);
      } else if (el.isContentEditable === true) {
        value = shownText(el);
      }
    }
    const expanded = lower(el, "aria-expanded");
    const out = {
      ref: refOf(el),
      tag,
      role: lower(el, "role").split(" ")[0] || "",
      name: accessibleName(el),
      type: tag === "input" || tag === "button" ? lower(el, "type") : "",
      href: (tag === "a" || tag === "area") && el.hasAttribute("href") ? absolute(attr(el, "href")) : "",
      value,
      aria_label: tidy(attr(el, "aria-label"), MAX_NAME),
      title_attr: tidy(attr(el, "title"), MAX_NAME),
      in_form: form !== null,
      form_method: method,
      form_action: action,
      form_submit_name: submitName,
      submits: submit,
      disabled: isDisabled(el),
      sensitive,
      aria_expanded: expanded === "true" ? true : expanded === "false" ? false : null,
      aria_haspopup: !["", "false"].includes(lower(el, "aria-haspopup")),
      contenteditable: el.isContentEditable === true,
      inside_dialog_title: dialogTitle(el),
    };
    if (tag === "input" && ["checkbox", "radio"].includes(lower(el, "type"))) out.checked = el.checked === true;
    if (tag === "select") {
      out.options = Array.from(el.options || []).slice(0, MAX_OPTIONS).map((option) => tidy(option.textContent || "", 60));
      out.more_options = Math.max(0, (el.options ? el.options.length : 0) - MAX_OPTIONS);
    }
    return out;
  };

  if (args.op === "describe") {
    if (!element) return { error: "no_such_element" };
    const out = facts(element);
    out.focused = element === element.ownerDocument.activeElement;
    return out;
  }

  if (args.op === "focused") {
    const active = document.hasFocus() ? document.activeElement : null;
    if (!active || active === document.body || active === document.documentElement) return { error: "no_focused_element" };
    if (active.tagName === "IFRAME" || active.tagName === "FRAME") return { error: "in_frame" };
    const out = facts(active);
    out.focused = true;
    return out;
  }

  if (args.op !== "snapshot") return { error: "bad_op" };

  // --- the whole page ---
  const shown = (el, style) => {
    if (style.visibility !== "visible") return false;
    const box = el.getBoundingClientRect();
    return box.width >= 1 && box.height >= 1;
  };
  const maxElements = Math.max(1, Math.min(Number(args.max_elements) || 400, 1000));
  let next = Math.max(1, Number(args.next_ref) || 1);
  // A new document has no labels yet, so numbering starts again at e1. browserd keeps one
  // counter per tab and hands it to each frame in turn; only the top frame may restart it.
  const marker = typeof args.doc === "string" ? args.doc.slice(0, 40) : "";
  const top = document.documentElement;
  if (top && marker) {
    if (args.main === true && attr(top, "data-ra-doc") !== marker) next = 1;
    top.setAttribute("data-ra-doc", marker);
  }
  const used = new Set();
  const elements = [];
  let listed = 0;
  let structure = 0;
  let visited = 0;
  let more = 0;
  let loginForm = false;

  const root = document.body || document.documentElement;
  const stack = root ? [[root, 0]] : [];
  while (stack.length) {
    const [el, depth] = stack.pop();
    visited += 1;
    if (visited > MAX_VISITED) { more += 1; break; }
    const tag = el.tagName;
    if (tag === "SCRIPT" || tag === "STYLE" || tag === "NOSCRIPT" || tag === "TEMPLATE" || tag === "HEAD") continue;
    let style;
    try { style = getComputedStyle(el); } catch (error) { continue; }
    if (style.display === "none") continue;
    let childDepth = depth;
    const visible = shown(el, style);
    if (visible) {
      const role = lower(el, "role").split(" ")[0];
      const heading = /^H[1-6]$/.test(tag) || role === "heading";
      const region = REGIONS[tag] || (REGION_ROLES.includes(role) ? role : "");
      // A link without an address is listed only when it has text: scripts often make
      // those clickable. An editing area is listed once, at its outermost element.
      const editor = el.isContentEditable === true && !(el.parentElement && el.parentElement.isContentEditable === true);
      const bareLink = tag === "A" && !el.hasAttribute("href") && shownText(el) !== "";
      const interactive = (el.matches(INTERACTIVE) || editor || bareLink)
        && !(tag === "INPUT" && lower(el, "type") === "hidden");
      if (interactive) {
        if (listed >= maxElements) {
          more += 1;
        } else {
          let ref = refOf(el);
          if (!ref || used.has(ref) || Number(ref.slice(1)) >= next) {
            ref = "e" + next;
            next += 1;
            el.setAttribute("data-ra-ref", ref);
          }
          used.add(ref);
          const item = facts(el);
          item.ref = ref;
          item.depth = depth;
          elements.push(item);
          listed += 1;
          if (item.sensitive && (item.type === "password" || lower(el, "autocomplete").includes("password"))) loginForm = true;
        }
      } else if ((heading || region) && structure < MAX_STRUCTURE) {
        const item = { role: heading ? "heading" : region, depth, name: heading
          ? shownText(el)
          : tidy(attr(el, "aria-label"), 80) || (region === "dialog" || region === "alertdialog" ? dialogTitle(el.firstElementChild || el) : "") };
        if (heading) {
          const level = /^H[1-6]$/.test(tag) ? Number(tag[1]) : Number(attr(el, "aria-level")) || 2;
          item.level = Math.max(1, Math.min(level, 6));
        }
        if (heading ? item.name : true) {
          elements.push(item);
          structure += 1;
          if (region) childDepth = depth + 1;
        }
      }
    }
    const kids = [];
    if (el.shadowRoot) kids.push(...el.shadowRoot.children);
    kids.push(...el.children);
    for (let i = kids.length - 1; i >= 0; i -= 1) stack.push([kids[i], Math.min(childDepth, 6)]);
  }

  const maxChars = Math.max(0, Math.min(Number(args.max_chars) || 0, 20000));
  let text = "";
  let cut = false;
  if (maxChars && document.body) {
    let raw = "";
    try { raw = document.body.innerText || ""; } catch (error) { raw = ""; }
    cut = raw.length > maxChars * 4;
    raw = raw.slice(0, maxChars * 4).replace(/[ \t\f\v ]+/g, " ").replace(/ ?\n ?/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
    cut = cut || raw.length > maxChars;
    text = wellFormed(raw.slice(0, maxChars));
  }

  return {
    elements,
    text,
    truncated: cut || more > 0,
    more_elements: more,
    next_ref: next,
    login_form_detected: loginForm,
  };
}
