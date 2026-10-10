"""Browser action classifier (§9.4.1): decides which clicks, keys and pages need Roland's OK.

Everything here is plain token comparison with no regex, so hostile page text can't slow it
down. The element descriptions come from browserd and ultimately from the web page, so they
are untrusted: a wrong guess must only ever cost an extra approval, never skip one.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from urllib.parse import urlsplit

MAX_FIELD_CHARS = 600  # each text field is cut to this before matching
MAX_URL_CHARS = 2048

# English + Finnish. Adding a keyword is fine; removing one needs Roland (docs/v2-spec.md §9.4.1).
KEYWORDS: dict[str, tuple[str, ...]] = {
    "payment": (
        "buy", "purchase", "order", "checkout", "check out", "pay", "payment", "place order",
        "add card", "subscribe", "upgrade", "donate", "book now", "reserve", "rent", "bid",
        "transfer", "send money", "withdraw",
        "osta", "tilaa", "maksa", "maksu", "kassalle", "vahvista tilaus", "varaa", "lahjoita",
        "siirrä", "tilisiirto",
    ),
    "message": (
        "send", "reply", "message", "email", "invite", "forward",
        "lähetä", "vastaa", "viesti", "kutsu",
    ),
    "public_post": (
        "post", "publish", "tweet", "share", "comment", "review", "upload", "retweet", "repost",
        "julkaise", "jaa", "kommentoi", "arvostele",
    ),
    "delete": (
        "delete", "remove", "erase", "close account", "deactivate", "unsubscribe",
        "cancel subscription", "revoke",
        "poista", "sulje tili", "peru tilaus",
    ),
    "form_submit": (
        "submit", "confirm", "apply", "sign", "accept", "agree", "save", "continue", "next",
        "register", "sign up",
        "lähetä lomake", "vahvista", "hyväksy", "tallenna", "jatka", "rekisteröidy",
    ),
}

# When several categories match, the most serious one wins.
PRECEDENCE = ("payment", "delete", "message", "public_post", "form_submit")

SAFE_ROLES = frozenset({"tab", "menuitem", "option", "combobox", "treeitem"})
TOGGLE_ROLES = frozenset({"checkbox", "radio", "switch"})
SEARCH_ROLES = frozenset({"searchbox"})

# Keys the agent may press (§8.4). Printable characters are left out on purpose: text goes
# through browser_type, which refuses password and code fields.
PLAIN_KEYS = frozenset({
    "Tab", "Shift+Tab", "Escape", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
    "PageUp", "PageDown", "Home", "End", "Backspace", "Delete",
})
SUBMIT_COMBOS = frozenset({"Control+Enter", "Meta+Enter"})
KEY_ALIASES = {
    "enter": "Enter", "return": "Enter", "space": "Space", "tab": "Tab",
    "shift+tab": "Shift+Tab", "escape": "Escape", "esc": "Escape",
    "arrowup": "ArrowUp", "arrowdown": "ArrowDown", "arrowleft": "ArrowLeft",
    "arrowright": "ArrowRight", "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft",
    "right": "ArrowRight", "pageup": "PageUp", "pagedown": "PageDown", "home": "Home",
    "end": "End", "backspace": "Backspace", "delete": "Delete",
    "control+enter": "Control+Enter", "ctrl+enter": "Control+Enter",
    "meta+enter": "Meta+Enter", "cmd+enter": "Meta+Enter",
}


KEY_NOT_ALLOWED = "that key isn't on the list of keys you may press"


@dataclass(frozen=True)
class Verdict:
    risk: str  # "safe" | "gated" | "forbidden"
    category: str = "other"
    why: str = ""  # short, code-made explanation for the approval card or the refusal
    keyword: str | None = None


def words(text: str) -> list[str]:
    """Lower-cased word tokens: every character that isn't a letter or digit splits a word."""
    text = unicodedata.normalize("NFKC", str(text or "")[:MAX_FIELD_CHARS * 4].lower()).lower()
    if not text.isascii():
        # Invisible characters (zero-width joiners, direction marks) must not split a word:
        # "pa\u200by" is still "pay" to whoever reads the button.
        text = "".join(char for char in text if unicodedata.category(char) != "Cf")
    text = " ".join(text.split())[:MAX_FIELD_CHARS]
    out: list[str] = []
    current: list[str] = []
    for char in text:
        if char.isalnum():
            current.append(char)
        elif current:
            out.append("".join(current))
            current = []
    if current:
        out.append("".join(current))
    return out


_KEYWORD_WORDS: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    category: tuple((keyword, tuple(words(keyword))) for keyword in keywords)
    for category, keywords in KEYWORDS.items()
}


def _matches(tokens: list[str], keyword: tuple[str, ...]) -> bool:
    """True when the keyword's words appear as consecutive tokens, each token starting with
    its keyword word: 'order' matches 'orders' and 'ordering' but not 'border'."""
    size = len(keyword)
    for start in range(len(tokens) - size + 1):
        if all(tokens[start + offset].startswith(keyword[offset]) for offset in range(size)):
            return True
    return False


def keyword_category(*texts: str) -> tuple[str, str] | None:
    """The most serious (category, keyword) found in any of the texts, or None.

    Each text is matched on its own with its own length cap, so padding one field can't push
    a keyword in another field past the cut-off.
    """
    token_lists = [tokens for tokens in (words(text) for text in texts if text) if tokens]
    if not token_lists:
        return None
    for category in PRECEDENCE:
        for keyword, keyword_words in _KEYWORD_WORDS[category]:
            if any(_matches(tokens, keyword_words) for tokens in token_lists):
                return category, keyword
    return None


def _text(element: dict, key: str) -> str:
    value = element.get(key)
    return value if isinstance(value, str) else ""


def _url_words(url: str, *, query: bool = True) -> str:
    """The path (and query) of a URL as text; the host name is left out so 'shop.example'
    doesn't gate every link on a shop."""
    if not url:
        return ""
    try:
        parts = urlsplit(url[:MAX_URL_CHARS])
    except ValueError:
        return url[:MAX_FIELD_CHARS]
    return f"{parts.path} {parts.query if query else ''}".strip()


def _is_http(url: str) -> bool:
    try:
        parts = urlsplit(url[:MAX_URL_CHARS])
    except ValueError:
        return False
    return parts.scheme in {"http", "https"} and bool(parts.hostname)


def element_texts(element: dict) -> list[str]:
    """Every piece of an element's description that could name a risky action."""
    return [
        _text(element, "name"),
        _text(element, "value"),
        _text(element, "aria_label"),
        # The element's own title attribute. Plain `title` in a browserd answer is the page
        # title, which must not count: a page called "Checkout" would gate every click on it.
        _text(element, "title_attr"),
        _url_words(_text(element, "href")),
        _url_words(_text(element, "form_action"), query=False),
        _text(element, "form_submit_name"),
    ]


def form_line(element: dict) -> str:
    """'POST shop.example/checkout' for the approval card, or '' when there is no form."""
    if not element.get("in_form"):
        return ""
    method = (_text(element, "form_method") or "get").upper()[:10]
    action = _text(element, "form_action")
    try:
        parts = urlsplit(action[:MAX_URL_CHARS])
        where = f"{parts.hostname or ''}{parts.path}"[:200]
    except ValueError:
        where = ""
    return f"{method} {where}".strip()


def is_submit_control(element: dict) -> bool:
    tag = _text(element, "tag").lower()
    kind = _text(element, "type").lower()
    if element.get("submits"):
        return True
    if kind == "submit":
        return True
    if tag == "input" and kind == "image":
        return True
    # A <button> inside a form with no type attribute submits the form.
    return tag == "button" and bool(element.get("in_form")) and kind == ""


def classify_click(element: dict) -> Verdict:
    """§9.4.1, rules 1-7 in order. Anything not positively known to be harmless is gated."""
    tag = _text(element, "tag").lower()
    role = _text(element, "role").lower()
    kind = _text(element, "type").lower()
    in_form = bool(element.get("in_form"))

    if element.get("disabled"):
        return Verdict("safe", why="disabled")

    found = keyword_category(*element_texts(element))
    if found:
        category, keyword = found
        return Verdict("gated", category, f"matched “{keyword}”", keyword)

    if is_submit_control(element):
        line = form_line(element)
        return Verdict("gated", "form_submit", f"submits form {line}".strip())

    # Following a link is a GET. A script that turns the click into a form POST is stopped
    # by browserd's POST-navigation guard, because safe clicks run in "safe" mode.
    if tag == "a" and _is_http(_text(element, "href")):
        return Verdict("safe", why="link")

    is_button = tag == "button" or role == "button"
    opens_something = element.get("aria_expanded") is not None or bool(element.get("aria_haspopup"))
    if (
        role in SAFE_ROLES
        or tag == "summary"
        or (is_button and opens_something)
        or (tag == "button" and kind == "button" and not in_form)
    ):
        return Verdict("safe", why="opens or switches a view")

    if role in TOGGLE_ROLES or (tag == "input" and kind in {"checkbox", "radio"}):
        if in_form:
            return Verdict("safe", why="form tick box")
        return Verdict("gated", "other", "tick box outside a form (it may save a setting at once)")

    return Verdict("gated", "other", "unrecognised element (default-deny)")


def classify_open(url: str) -> Verdict:
    """browser_open: only http(s); a risky word in the path or query asks Roland first."""
    if not isinstance(url, str) or not url.strip():
        return Verdict("forbidden", why="give a full http or https address")
    url = url.strip()
    if len(url) > MAX_URL_CHARS:
        return Verdict("forbidden", why=f"the address is longer than {MAX_URL_CHARS} characters")
    try:
        parts = urlsplit(url)
    except ValueError:
        return Verdict("forbidden", why="that is not a valid address")
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return Verdict("forbidden", why="only http and https addresses can be opened")
    if parts.username is not None or parts.password is not None:
        return Verdict("forbidden", why="addresses with a user name or password aren't allowed")
    found = keyword_category(f"{parts.path} {parts.query}")
    if found:
        return Verdict("gated", "other", f"the address contains “{found[1]}”", found[1])
    return Verdict("safe")


def normal_key(key: str) -> str | None:
    """The canonical name of an allowed key, or None when the key isn't on the allowlist."""
    if not isinstance(key, str) or len(key) > 20:
        return None
    if key == " ":
        return "Space"
    return KEY_ALIASES.get(key.lower().replace(" ", ""))


def _is_search_box(element: dict) -> bool:
    role = _text(element, "role").lower()
    kind = _text(element, "type").lower()
    if role not in SEARCH_ROLES and kind != "search":
        return False
    method = (_text(element, "form_method") or "get").lower()
    return not element.get("in_form") or method == "get"


def _is_button_like(element: dict) -> bool:
    tag = _text(element, "tag").lower()
    role = _text(element, "role").lower()
    kind = _text(element, "type").lower()
    return tag == "button" or role == "button" or kind in {"submit", "image", "button", "reset"}


_NOT_TEXT_INPUTS = frozenset({
    "checkbox", "radio", "submit", "image", "button", "reset", "file", "range", "color",
})


def _is_text_entry(element: dict) -> bool:
    """A field where Space just types a space."""
    tag = _text(element, "tag").lower()
    role = _text(element, "role").lower()
    if tag == "textarea" or role in {"textbox", "searchbox"} or element.get("contenteditable"):
        return True
    return tag == "input" and _text(element, "type").lower() not in _NOT_TEXT_INPUTS


def is_sensitive(element: dict) -> bool:
    """browserd marks password, code and card fields. A password input counts either way."""
    return bool(element.get("sensitive")) or _text(element, "type").lower() == "password"


def classify_press(key: str, focused: dict | None) -> Verdict:
    """browser_press: Enter usually submits something, so it needs approval except in a
    search box. `focused` is the element that has the keyboard focus, or None if unknown."""
    name = normal_key(key)
    if name is None:
        return Verdict("forbidden", why=KEY_NOT_ALLOWED)
    if name in SUBMIT_COMBOS:
        return Verdict("gated", "form_submit", f"{name} sends or submits on most pages")
    if name in PLAIN_KEYS:
        return Verdict("safe")
    if focused is None:
        if name == "Enter":
            return Verdict("gated", "form_submit", "Enter was pressed and the focused element is unknown")
        return Verdict("safe")  # Space with nothing focused only scrolls the page
    if name == "Space" and not _is_button_like(focused):
        if _is_text_entry(focused):
            return Verdict("safe")
        # Space activates what has the focus (a tick box, a custom control): same as a click.
        as_click = classify_click(focused)
        return as_click if as_click.risk == "gated" else Verdict("safe")
    if name == "Enter" and _is_search_box(focused):
        return Verdict("safe", why="search box")
    found = keyword_category(*element_texts(focused))
    if found:
        return Verdict("gated", found[0], f"matched “{found[1]}”", found[1])
    line = form_line(focused)
    why = f"submits form {line}" if line else f"{name} activates the focused element"
    return Verdict("gated", "form_submit", why)


def classify_type(element: dict, *, submit: bool) -> Verdict:
    """browser_type: never into password, code or card fields; submitting needs approval."""
    if is_sensitive(element):
        return Verdict(
            "forbidden",
            why="that is a password, code or card field. Never type into it; tell Roland he "
                "has to sign in himself",
        )
    if not submit:
        return Verdict("safe")
    found = keyword_category(
        _url_words(_text(element, "form_action"), query=False),
        _text(element, "form_submit_name"),
    )
    line = form_line(element)
    why = f"submits form {line}" if line else "types and then submits"
    if found:
        return Verdict("gated", found[0], f"{why} · matched “{found[1]}”", found[1])
    return Verdict("gated", "form_submit", why)
