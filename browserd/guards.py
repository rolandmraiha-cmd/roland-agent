"""Pure checks shared by browserd's server and session: addresses, keys, file names and the
element fingerprint. No browser is needed to test these."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import unicodedata
from urllib.parse import unquote, urlsplit

from .settings import MAX_URL_CHARS

# IPv6 ranges that can wrap an IPv4 address (NAT64, 6to4), so a private IPv4 could hide inside.
_WRAPPING_NETS = [ipaddress.ip_network(net) for net in ("64:ff9b::/96", "64:ff9b:1::/48", "2002::/16")]
_PRIVATE_SUFFIXES = (".localhost", ".internal", ".local", ".lan", ".home.arpa")

# Keys core may ask for, in the canonical form it sends (§8.4), and what Playwright calls them.
KEYS = {
    "Enter": "Enter", "Space": "Space", "Tab": "Tab", "Shift+Tab": "Shift+Tab", "Escape": "Escape",
    "ArrowUp": "ArrowUp", "ArrowDown": "ArrowDown", "ArrowLeft": "ArrowLeft",
    "ArrowRight": "ArrowRight", "PageUp": "PageUp", "PageDown": "PageDown", "Home": "Home",
    "End": "End", "Backspace": "Backspace", "Delete": "Delete",
    "Control+Enter": "Control+Enter", "Meta+Enter": "Meta+Enter",
}
APPROVED_ONLY_KEYS = frozenset({"Control+Enter", "Meta+Enter"})

# Every element fact the classifier in core reads. The fingerprint covers all of them, so a
# change in any one between "looked at" and "acted on" is noticed (docs/NEXT.md, contract 2).
FINGERPRINT_KEYS = (
    "tag", "role", "name", "type", "href", "form_method", "form_action", "in_form",
    "value", "aria_label", "title_attr", "form_submit_name", "submits", "disabled", "sensitive",
    "aria_expanded", "aria_haspopup", "contenteditable", "inside_dialog_title",
)


def fingerprint(element: dict) -> str:
    facts = [element.get(key) for key in FINGERPRINT_KEYS]
    return hashlib.sha256(json.dumps(facts, ensure_ascii=True, sort_keys=True).encode("ascii")).hexdigest()


def valid_ref(ref: object) -> bool:
    return (
        isinstance(ref, str)
        and 2 <= len(ref) <= 7
        and ref[0] == "e"
        and ref[1:].isascii()
        and ref[1:].isdigit()
    )


def valid_tab(tab_id: object) -> bool:
    return (
        isinstance(tab_id, str)
        and 2 <= len(tab_id) <= 8
        and tab_id[0] == "t"
        and tab_id[1:].isascii()
        and tab_id[1:].isdigit()
    )


def _ends_in_a_number(name: str) -> bool:
    """A browser reads a host whose last part is a number as an IPv4 address, in forms Python
    doesn't: "127.1", "2130706433", "0x7f.0.0.1", "0177.0.0.1"."""
    last = name.rsplit(".", 1)[-1]
    if last.isdigit():
        return True
    return last[:2] == "0x" and all(char in "0123456789abcdef" for char in last[2:])


def host_block_reason(host: str | None, allowed: frozenset[str] = frozenset()) -> str | None:
    """Why this host may not be contacted, or None when it may.

    Only what can be told from the name itself is checked here: literal private addresses,
    localhost, internal suffixes and single-label names (Docker service names). A public name
    that resolves to a private address is stopped by the host firewall, which is the
    authoritative layer (§5.3).
    """
    if not host:
        return "no_host"
    # Read the name the way a browser will: percent-escapes undone, look-alike digits and
    # dots folded to the plain ones.
    name = unicodedata.normalize("NFKC", unquote(host)).replace("\u3002", ".").replace("\uff61", ".")
    name = name.strip().lower().rstrip(".")
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    if not name:
        return "no_host"
    if name in allowed:
        return None
    try:
        address = ipaddress.ip_address(name.split("%")[0])
    except ValueError:
        address = None
    if address is not None:
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if not address.is_global or address.is_multicast or any(address in net for net in _WRAPPING_NETS):
            return "private_address"
        return None
    if ":" in name or "%" in name or _ends_in_a_number(name):
        return "private_address"  # an address written in a form we don't read: refuse it
    if name == "localhost" or name.endswith(_PRIVATE_SUFFIXES) or "." not in name:
        return "private_address"
    return None


def url_block_reason(url: object, allowed: frozenset[str] = frozenset()) -> str | None:
    """Why the browser may not request this address, or None when it may. http and https only."""
    if not isinstance(url, str) or not url:
        return "bad_url"
    if len(url) > MAX_URL_CHARS:
        return "too_long"
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return "bad_url"
    if parts.scheme not in {"http", "https"}:
        return "scheme"
    if parts.username is not None or parts.password is not None:
        return "credentials_in_url"
    return host_block_reason(host, allowed)


def main_frame_url_ok(url: str) -> bool:
    """Where a tab may end up. Error pages and blank pages are fine; local files, browser
    pages and source views are not."""
    scheme = url.partition(":")[0].lower()
    return scheme in {"http", "https", "about", "blob", "data", "chrome-error"}


def safe_download_name(suggested: object) -> str:
    """A plain file name for a downloaded file. The name comes from the website."""
    text = unicodedata.normalize("NFC", str(suggested or ""))
    text = text.replace("\\", "/").rsplit("/", 1)[-1]
    kept = "".join(
        char for char in text
        if unicodedata.category(char)[0] != "C" and char not in '<>:"|?*\x7f'
    ).strip().lstrip(".").strip()
    if not kept:
        return "download"
    stem, dot, extension = kept.rpartition(".")
    if dot and stem and len(extension) <= 16:
        while len((stem + dot + extension).encode("utf-8")) > 120 and len(stem) > 1:
            stem = stem[:-1]
        return stem + dot + extension
    while len(kept.encode("utf-8")) > 120:
        kept = kept[:-1]
    return kept


def valid_upload_name(name: object) -> bool:
    """Core stages uploads as a plain file name in the uploads folder; nothing else is read."""
    return (
        isinstance(name, str)
        and 1 <= len(name.encode("utf-8", "replace")) <= 255
        and "/" not in name
        and "\\" not in name
        and "\x00" not in name
        and name not in {".", ".."}
        and not name.startswith(".")
        and all(unicodedata.category(char)[0] != "C" for char in name)
    )


def clean(value):
    """Make text from the page safe to put in JSON: lone surrogates would break UTF-8."""
    if isinstance(value, str):
        return value.encode("utf-8", "replace").decode("utf-8") if not value.isascii() else value
    if isinstance(value, list):
        return [clean(item) for item in value]
    if isinstance(value, dict):
        return {clean(key): clean(item) for key, item in value.items()}
    return value
