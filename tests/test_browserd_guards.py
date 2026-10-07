"""browserd's pure checks: addresses, the element fingerprint, keys and file names (§6.5)."""

from __future__ import annotations

import fnmatch
import json

import pytest

from browserd import guards
from browserd.settings import MAX_URL_CHARS

PRIVATE = [
    "http://10.77.1.10:8080/", "http://10.0.0.1/", "http://172.16.0.1/", "http://172.31.255.255/",
    "http://192.168.1.1/", "http://127.0.0.1/", "http://127.8.8.8:9/", "http://169.254.169.254/latest/",
    "http://100.64.0.1/", "http://100.127.255.254/", "http://0.0.0.0/", "http://224.0.0.1/",
    "http://255.255.255.255/", "http://192.0.2.1/", "http://198.18.0.1/",
    "http://localhost/", "http://LOCALHOST:8080/", "http://localhost./", "http://app.localhost/",
    "http://core/", "http://core:8080/", "http://model:8000/", "http://fixture-web:8080/",
    "http://db.internal/", "http://printer.local/", "http://nas.lan/", "http://router.home.arpa/",
    "http://[::1]/", "http://[::]/", "http://[fc00::1]/", "http://[fd12:3456::1]/", "http://[fe80::1]/",
    "http://[::ffff:10.0.0.1]/", "http://[::ffff:127.0.0.1]/", "http://[64:ff9b::a00:1]/",
    "http://[2002:a00:1::]/", "http://[2001:db8::1]/",
    # Forms a browser reads as an IPv4 address even though they don't look like one.
    "http://127.1/", "http://2130706433/", "http://0x7f.0.0.1/", "http://0177.0.0.1/", "http://0x7f000001/",
    "http://１２７.0.0.1/", "http://%31%32%37.0.0.1/", "http://127.0.0.1./", "http://a.b.c.123/",
    "http://127。0。0。1/",
]
PUBLIC = [
    "https://example.com/", "http://example.com:8080/path?q=1#top", "https://www.hs.fi/",
    "https://xn--bcher-kva.example/", "https://bücher.example/", "http://1.1.1.1/", "https://8.8.8.8/dns",
    "http://[2606:4700:4700::1111]/", "https://example.com./", "https://shop.example.co.uk/a%20b",
    "https://10minutemail.example/", "https://localhost.example.com/", "https://my-local.example/",
]


@pytest.mark.parametrize("url", PRIVATE)
def test_private_and_local_addresses_are_refused(url):
    assert guards.url_block_reason(url) == "private_address"


@pytest.mark.parametrize("url", PUBLIC)
def test_public_addresses_are_allowed(url):
    assert guards.url_block_reason(url) is None


@pytest.mark.parametrize(("url", "reason"), [
    ("file:///etc/passwd", "scheme"), ("chrome://settings", "scheme"), ("about:blank", "scheme"),
    ("javascript:alert(1)", "scheme"), ("data:text/html,<p>x", "scheme"), ("view-source:https://example.com", "scheme"),
    ("ftp://example.com/x", "scheme"), ("ws://example.com/", "scheme"), ("blob:https://example.com/1", "scheme"),
    ("example.com", "scheme"), ("//example.com/x", "scheme"),
    ("https://roland:secret@example.com/", "credentials_in_url"), ("https://roland@example.com/", "credentials_in_url"),
    ("https://@example.com/", "credentials_in_url"),
    ("http:///nothing", "no_host"), ("https://", "no_host"), ("http://[not-an-address]/", "bad_url"),
    ("", "bad_url"), (None, "bad_url"), (12, "bad_url"), (["https://example.com/"], "bad_url"),
    ("https://example.com/" + "a" * MAX_URL_CHARS, "too_long"),
])
def test_other_addresses_that_are_refused(url, reason):
    assert guards.url_block_reason(url) == reason


def test_scheme_is_read_the_way_a_browser_reads_it():
    assert guards.url_block_reason("HTTPS://Example.COM/") is None
    assert guards.url_block_reason("FILE:///etc/passwd") == "scheme"
    assert guards.url_block_reason("hTtP://LocalHost/") == "private_address"


def test_a_test_stack_can_allow_its_fixture_site_by_name_only():
    allowed = frozenset({"fixture-web", "127.0.0.1"})
    assert guards.url_block_reason("http://fixture-web:8080/shop", allowed) is None
    assert guards.url_block_reason("http://127.0.0.1:18080/", allowed) is None
    assert guards.url_block_reason("http://127.0.0.2:18080/", allowed) == "private_address"
    assert guards.url_block_reason("http://core:8080/", allowed) == "private_address"
    assert guards.url_block_reason("file:///etc/passwd", allowed) == "scheme"


def _resolver_patterns(rules: str) -> tuple[list[str], list[str]]:
    blocked, excluded = [], []
    for rule in rules.split(", "):
        words = rule.split(" ")
        if words[0] == "MAP":
            assert words[2:] == ["~NOTFOUND"], rule
            blocked.append(words[1])
        else:
            assert words[0] == "EXCLUDE" and len(words) == 2, rule
            excluded.append(words[1])
    return blocked, excluded


def test_resolver_rules_cover_what_a_redirect_could_point_at():
    """Chromium matches these patterns against the host of every request, redirects included."""
    blocked, excluded = _resolver_patterns(guards.resolver_rules())
    assert excluded == []

    def stopped(host: str) -> bool:
        return any(fnmatch.fnmatchcase(host, pattern) for pattern in blocked)

    for host in ("localhost", "10.77.1.10", "10.0.0.1", "127.0.0.1", "127.9.9.9", "169.254.169.254",
                 "192.168.0.10", "172.16.0.1", "172.31.9.9", "100.64.0.1", "100.127.0.1", "0.0.0.0",
                 "::1", "::", "fd00::1", "fc00::1", "fe80::1", "::ffff:a00:1", "64:ff9b::a00:1",
                 "db.internal", "printer.local", "nas.lan", "router.home.arpa", "app.localhost"):
        assert stopped(host), host
    for host in ("example.com", "1.1.1.1", "172.15.0.1", "172.32.0.1", "100.63.0.1", "100.128.0.1",
                 "192.169.0.1", "2606:4700:4700::1111", "localhost.example.com", "fd.example.com"):
        assert not stopped(host), host
    # Nothing the rule stops is something the request guard would have let through.
    v6 = {"fc*:*": "fc00::1", "fd*:*": "fd00::1", "fe8*:*": "fe80::1", "fe9*:*": "fe90::1",
          "fea*:*": "fea0::1", "feb*:*": "feb0::1", "::ffff:*": "::ffff:a00:1",
          "64:ff9b:*": "64:ff9b::a00:1", "2002:*": "2002:a00:1::"}
    for pattern in blocked:
        host = v6.get(pattern) or ("x" + pattern[1:] if pattern.startswith("*.") else pattern.replace("*", "1"))
        url = f"http://[{host}]/" if ":" in host else f"http://{host}/"
        assert guards.url_block_reason(url) == "private_address", pattern


def test_resolver_rules_leave_the_fixture_site_reachable_in_tests():
    rules = guards.resolver_rules(frozenset({"fixture-web", "127.0.0.1", "bad name, MAP * 1.2.3.4", ""}))
    blocked, excluded = _resolver_patterns(rules)
    assert excluded == ["127.0.0.1", "fixture-web"]  # the odd entry can't add a rule of its own
    assert "127.*.*.*" in blocked


def test_main_frame_may_only_show_web_pages_and_blank_ones():
    for url in ("https://example.com/", "http://example.com/", "about:blank", "chrome-error://chromewebdata/",
                "data:text/html,x", "blob:https://example.com/1"):
        assert guards.main_frame_url_ok(url), url
    for url in ("file:///etc/passwd", "chrome://settings", "view-source:https://example.com/",
                "devtools://devtools/bundled/inspector.html", "chrome-extension://abc/x.html", "javascript:1", ""):
        assert not guards.main_frame_url_ok(url), url


ELEMENT = {
    "ref": "e3", "tag": "button", "role": "", "name": "Place order", "type": "submit", "href": "",
    "form_method": "post", "form_action": "https://shop.example/order", "in_form": True, "value": "",
    "aria_label": "", "title_attr": "", "form_submit_name": "Place order", "submits": True,
    "disabled": False, "sensitive": False, "aria_expanded": None, "aria_haspopup": False,
    "contenteditable": False, "inside_dialog_title": "",
}


def test_fingerprint_changes_with_every_fact_the_classifier_reads():
    base = guards.fingerprint(ELEMENT)
    assert len(base) == 64 and base == guards.fingerprint(dict(ELEMENT))
    changes = {
        "tag": "a", "role": "link", "name": "Place 0rder", "type": "button", "href": "https://x.example/",
        "form_method": "get", "form_action": "https://evil.example/order", "in_form": False, "value": "1",
        "aria_label": "Delete account", "title_attr": "Pay now", "form_submit_name": "Delete", "submits": False,
        "disabled": True, "sensitive": True, "aria_expanded": True, "aria_haspopup": True,
        "contenteditable": True, "inside_dialog_title": "Confirm payment",
    }
    assert set(changes) == set(guards.FINGERPRINT_KEYS)
    seen = {base}
    for key, value in changes.items():
        seen.add(guards.fingerprint({**ELEMENT, key: value}))
    assert len(seen) == len(changes) + 1
    # Where the element is, and what a snapshot adds for display, is not part of it.
    assert guards.fingerprint({**ELEMENT, "ref": "e9", "depth": 3, "checked": True, "options": ["a"]}) == base


def test_fingerprint_covers_exactly_the_facts_core_classifies():
    """Core's own list of what it classifies (agent/tools_browser.py) must be inside ours:
    a fact core reads but browserd didn't fingerprint could change unnoticed."""
    from agent import tools_browser

    assert set(tools_browser._SEEN_KEYS) - {"url"} <= set(guards.FINGERPRINT_KEYS)


def test_fingerprint_handles_text_json_cannot_hold():
    odd = {**ELEMENT, "name": "half \ud800 pair", "aria_label": "\u202e\x00"}
    assert len(guards.fingerprint(odd)) == 64


@pytest.mark.parametrize("ref", ["e1", "e42", "e999999"])
def test_valid_refs(ref):
    assert guards.valid_ref(ref)


@pytest.mark.parametrize("ref", ["", "e", "1", "E1", "e-1", "e1.5", "e1 ", " e1", "e१", "e1234567", "x1", "e1\n", None, 1,
                                 "e1\"]", "e1,e2", ["e1"]])
def test_invalid_refs(ref):
    assert not guards.valid_ref(ref)


def test_tab_ids():
    assert guards.valid_tab("t1") and guards.valid_tab("t1234567")
    for bad in ("", "t", "1", "T1", "t-1", "t12345678", "t1/close", "../t1", "t٣", None, 1):
        assert not guards.valid_tab(bad), bad


def test_only_the_spec_keys_can_be_pressed():
    assert set(guards.KEYS) == {
        "Enter", "Space", "Tab", "Shift+Tab", "Escape", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
        "PageUp", "PageDown", "Home", "End", "Backspace", "Delete", "Control+Enter", "Meta+Enter",
    }
    assert guards.APPROVED_ONLY_KEYS == {"Control+Enter", "Meta+Enter"}
    for combo in ("Control+L", "Control+T", "Control+Shift+I", "F12", "Alt+ArrowLeft", "Control+V", "Control+A"):
        assert combo not in guards.KEYS


@pytest.mark.parametrize(("suggested", "expected"), [
    ("report.csv", "report.csv"), ("../../etc/passwd", "passwd"), ("..\\..\\boot.ini", "boot.ini"),
    ("/abs/path/file.pdf", "file.pdf"), (".bashrc", "bashrc"), ("...", "download"), ("", "download"),
    (None, "download"), ("a\x00b\nc.txt", "abc.txt"), ("invoice\u202egnp.exe", "invoicegnp.exe"),
    ('we<i>rd:"na|me?*.txt', "weirdname.txt"), ("  spaced name .txt ", "spaced name .txt"),
    ("résumé.pdf", "résumé.pdf"), ("C:\\Users\\x\\file.txt", "file.txt"),
])
def test_download_names_are_plain_file_names(suggested, expected):
    assert guards.safe_download_name(suggested) == expected


def test_long_download_names_are_cut_but_keep_their_ending():
    name = guards.safe_download_name("x" * 400 + ".tar.gz")
    assert name.endswith(".gz") and len(name.encode()) <= 120
    assert len(guards.safe_download_name("ä" * 400).encode()) <= 120
    assert "/" not in guards.safe_download_name("a/" * 200 + "end.txt")


def test_upload_names():
    for good in ("note.txt", "upload-note.txt", "résumé.pdf", "a b.c", "x"):
        assert guards.valid_upload_name(good), good
    for bad in ("", ".", "..", ".hidden", "../x", "a/b", "a\\b", "a\x00b", "a\nb", "x" * 256, "/etc/passwd", None, 3,
                "ok\u202e.txt"):
        assert not guards.valid_upload_name(bad), bad


def test_clean_makes_page_text_safe_to_send():
    dirty = {"name": "half \ud800 pair", "list": ["ok", "\udc00"], "n": 3, "none": None, "\ud800key": True}
    cleaned = guards.clean(dirty)
    json.dumps(cleaned).encode("utf-8")
    json.dumps(cleaned, ensure_ascii=False).encode("utf-8")  # would fail on a lone surrogate
    assert cleaned["name"] == "half ? pair" and cleaned["list"] == ["ok", "?"] and cleaned["n"] == 3
    assert guards.clean("plain") == "plain" and guards.clean("päivää") == "päivää"
