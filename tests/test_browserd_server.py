"""browserd's HTTP layer and the parts of its session that need no browser (§6.5, §8.4).

The session here is a stand-in that records what it was asked. The real one, with a real
Chromium, is covered by tests/integration/test_browser_live.py.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport

from browserd import guards, launcher, session, settings
from browserd.server import OPEN_IN_USER_MODE, create_app
from browserd.session import BrowserdError

TOKEN = "unit-test-token-0123456789"
CORE = "10.77.4.10"
PRINT = "f" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

# §8.4, and nothing else. A new route here needs Roland (docs/NEXT.md, M6 security requirements).
ROUTES = {
    ("GET", "/healthz"), ("GET", "/v1/status"), ("GET", "/v1/downloads"),
    ("POST", "/v1/navigate"), ("POST", "/v1/snapshot"), ("POST", "/v1/describe"), ("POST", "/v1/click"),
    ("POST", "/v1/type"), ("POST", "/v1/press"), ("POST", "/v1/select"), ("POST", "/v1/scroll"),
    ("POST", "/v1/back"), ("POST", "/v1/forward"), ("POST", "/v1/tabs/{tab_id}/activate"),
    ("POST", "/v1/tabs/{tab_id}/close"), ("POST", "/v1/screenshot"), ("POST", "/v1/upload"),
    ("POST", "/v1/user-mode"), ("POST", "/v1/vnc/disconnect"),
}
# One valid request per action route, to check each is locked in user mode and reaches the session.
ACTIONS = [
    ("POST", "/v1/navigate", {"url": "https://example.com/"}),
    ("POST", "/v1/snapshot", {"max_chars": 100}),
    ("POST", "/v1/describe", {"ref": "e1"}),
    ("POST", "/v1/click", {"ref": "e1", "fingerprint": PRINT, "mode": "safe"}),
    ("POST", "/v1/type", {"ref": "e1", "fingerprint": PRINT, "text": "hello", "mode": "safe"}),
    ("POST", "/v1/press", {"key": "Enter", "mode": "safe"}),
    ("POST", "/v1/select", {"ref": "e1", "fingerprint": PRINT, "values": ["a"]}),
    ("POST", "/v1/scroll", {"direction": "down", "pages": 1}),
    ("POST", "/v1/back", None), ("POST", "/v1/forward", None),
    ("POST", "/v1/tabs/t1/activate", None), ("POST", "/v1/tabs/t1/close", None),
    ("POST", "/v1/screenshot", {"full_page": False}),
    ("POST", "/v1/upload", {"ref": "e1", "fingerprint": PRINT, "path": "a.txt", "sha256": "0" * 64}),
    ("GET", "/v1/downloads", None),
]


class FakeSession:
    """Answers like browserd.session.Session and remembers the calls."""

    def __init__(self):
        self.mode = "agent"
        self.calls: list[tuple] = []
        self.fail: Exception | None = None
        self.page = {"ok": True, "mode": "agent", "url": "https://example.com/", "title": "Example"}

    def _did(self, *call):
        self.calls.append(call)
        if self.fail is not None:
            raise self.fail
        return dict(self.page)

    def health(self):
        return {"ok": True, "xvfb": True, "vnc": False, "browser": True}

    async def status(self):
        return {"mode": self.mode, "tabs": [], "url": "", "title": ""}

    async def set_user_mode(self, on):
        self.calls.append(("user_mode", on))
        self.mode = "user" if on else "agent"
        return {"mode": self.mode}

    async def navigate(self, url, new_tab):
        return self._did("navigate", url, new_tab)

    async def snapshot(self, max_chars):
        return self._did("snapshot", max_chars)

    async def describe(self, ref=None, focused=False):
        return self._did("describe", ref, focused)

    async def click(self, ref, fingerprint, mode):
        return self._did("click", ref, fingerprint, mode)

    async def type(self, ref, fingerprint, text, *, clear, submit, mode):
        return self._did("type", ref, fingerprint, text, clear, submit, mode)

    async def press(self, key, mode, fingerprint=None):
        return self._did("press", key, mode, fingerprint)

    async def select(self, ref, fingerprint, values, mode):
        return self._did("select", ref, fingerprint, values, mode)

    async def scroll(self, direction, pages):
        return self._did("scroll", direction, pages)

    async def history(self, forward):
        return self._did("history", forward)

    async def activate_tab(self, tab_id):
        return self._did("activate", tab_id)

    async def close_tab(self, tab_id):
        return self._did("close", tab_id)

    async def screenshot(self, full_page):
        self._did("screenshot", full_page)
        return PNG

    async def upload(self, ref, fingerprint, name, sha256):
        return self._did("upload", ref, fingerprint, name, sha256)

    async def downloads(self):
        self._did("downloads")
        return [{"name": "report.csv", "size": 3, "finished": True}]


def make(**changes):
    fake = FakeSession()
    config = settings.Settings(token=TOKEN, **changes)
    return create_app(config, fake), fake


def client(app, peer: str = CORE, token: str | None = TOKEN) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return httpx.AsyncClient(
        transport=ASGITransport(app=app, client=(peer, 40000)), base_url="http://browser.test",
        headers=headers, trust_env=False,
    )


async def send(http: httpx.AsyncClient, method: str, path: str, body):
    return await http.request(method, path, json=body) if body is not None else await http.request(method, path)


# --- which routes exist ---

def test_the_route_table_is_exactly_the_spec():
    app, _ = make()
    found = {(method, route.path) for route in app.routes for method in route.methods - {"HEAD", "OPTIONS"}}
    assert found == ROUTES
    assert {path for _, path, _ in ACTIONS} | OPEN_IN_USER_MODE >= {
        path.replace("{tab_id}", "t1") for _, path in ROUTES
    }  # the checks below cover every route


async def test_nothing_reads_cookies_storage_or_runs_script():
    app, fake = make()
    async with client(app) as http:
        for path in ("/v1/cookies", "/v1/storage", "/v1/evaluate", "/v1/eval", "/v1/cdp", "/v1/har", "/v1/trace",
                     "/v1/clipboard", "/v1/profile", "/v1/values", "/json/version", "/devtools/browser", "/docs",
                     "/redoc", "/openapi.json", "/v1", "/"):
            for method in ("GET", "POST", "PUT", "DELETE"):
                assert (await http.request(method, path)).status_code in {404, 405}, (method, path)
    assert fake.calls == []


def test_the_browser_is_started_without_a_debugging_port_or_recording():
    source = Path(session.__file__).read_text(encoding="utf-8")
    for word in ("remote-debugging", "connect_over_cdp", "new_cdp_session", "tracing", "record_video", "record_har",
                 "add_init_script", "expose_function", "expose_binding", "storage_state", ".cookies(",
                 "add_cookies", "aria_snapshot", "input_value", "inner_html", ".content("):
        assert word not in source, word
    assert not any("debug" in arg for arg in session.CHROMIUM_ARGS)
    # The only script that ever runs in a page is snapshot.js and three fixed one-liners.
    assert source.count(".evaluate(") == 6
    assert "service_workers=\"block\"" in source and "--host-resolver-rules=" in source


def test_the_page_script_only_reads_and_only_labels():
    script = (Path(session.__file__).parent / "snapshot.js").read_text(encoding="utf-8")
    for word in ("document.cookie", "localStorage", "sessionStorage", "indexedDB", "fetch(", "XMLHttpRequest",
                 "eval(", "Function(", "addEventListener", "sendBeacon", "WebSocket", "postMessage", "innerHTML",
                 "outerHTML", ".click(", ".submit(", ".focus(", "import(", "navigator.", "window.open"):
        assert word not in script, word
    assert script.count(".value") == 1  # the one place a field's value is read, after the secret check
    assert script.count("setAttribute(") == 2 and "removeAttribute" not in script
    assert all("data-ra-" in line for line in script.splitlines() if "setAttribute(" in line)


# --- who may call ---

async def test_only_core_may_call_and_only_with_the_token():
    app, fake = make()
    for peer in ("10.77.4.99", "10.77.12.40", "10.77.5.30", "192.168.1.5", "8.8.8.8", ""):
        async with client(app, peer=peer) as http:
            assert (await http.get("/healthz")).status_code == 403
            assert (await http.get("/v1/status")).status_code == 403
            assert (await http.post("/v1/navigate", json={"url": "https://example.com/"})).status_code == 403
    for token in (None, "", "wrong", TOKEN[:-1], TOKEN + "x", TOKEN.upper()):
        async with client(app, token=token) as http:
            assert (await http.get("/healthz")).status_code == 200  # the health check has no token
            assert (await http.get("/v1/status")).status_code == 401
            assert (await http.post("/v1/click", json=ACTIONS[3][2])).status_code == 401
    async with client(app, token=None) as http:
        for header in (TOKEN, f"bearer {TOKEN}", f"Bearer  {TOKEN}", f"Basic {TOKEN}", f"Bearer {TOKEN}é"):
            answer = await http.get("/v1/status", headers={"Authorization": header.encode("utf-8")})
            assert answer.status_code == 401, header
    assert fake.calls == []
    async with client(app) as http:
        assert (await http.get("/v1/status")).status_code == 200
    for peer in ("127.0.0.1", "::1", "10.77.4.40"):  # the container's own health check
        async with client(app, peer=peer, token=None) as http:
            assert (await http.get("/healthz")).json()["ok"] is True
            assert (await http.get("/v1/status")).status_code == 401


async def test_large_bodies_are_refused_before_they_are_read():
    app, fake = make()
    async with client(app) as http:
        big = await http.post("/v1/type", content=b"x" * (settings.MAX_BODY + 1))
        assert big.status_code == 413

        async def drip():
            for _ in range(70):
                yield b"y" * 1024

        assert (await http.post("/v1/type", content=drip())).status_code == 413
        lying = await http.post("/v1/type", content=b"{}", headers={"Content-Length": str(10**9)})
        assert lying.status_code in {400, 413}
    assert fake.calls == []


# --- user mode ---

async def test_every_action_is_locked_while_roland_has_the_browser():
    app, fake = make()
    async with client(app) as http:
        assert (await http.post("/v1/user-mode", json={"on": True, "reason": "sign in"})).json() == {"mode": "user"}
        for method, path, body in ACTIONS:
            answer = await send(http, method, path, body)
            assert (answer.status_code, answer.json()) == (423, {"error": "user_mode"}), path
        assert fake.calls == [("user_mode", True)]
        assert (await http.get("/v1/status")).json()["mode"] == "user"
        assert (await http.get("/healthz")).status_code == 200
        assert (await http.post("/v1/vnc/disconnect")).json() == {"ok": True}
        assert (await http.post("/v1/user-mode", json={"on": False})).json() == {"mode": "agent"}
        for method, path, body in ACTIONS:
            assert (await send(http, method, path, body)).status_code == 200, path
    assert len(fake.calls) == 2 + len(ACTIONS)


async def test_user_mode_needs_a_real_yes_or_no():
    app, fake = make()
    async with client(app) as http:
        for body in ({}, {"on": "true"}, {"on": 1}, {"on": None}, []):
            assert (await http.post("/v1/user-mode", json=body)).status_code == 400
    assert fake.calls == []


# --- what reaches the session ---

async def test_valid_requests_reach_the_session_as_sent():
    app, fake = make()
    async with client(app) as http:
        for method, path, body in ACTIONS:
            assert (await send(http, method, path, body)).status_code == 200, path
        await http.post("/v1/navigate", json={"url": "  https://example.com/x  ", "new_tab": True})
        await http.post("/v1/describe", json={"focused": True})
        await http.post("/v1/type", json={"ref": "e2", "fingerprint": PRINT, "text": "", "clear": False,
                                          "submit": True, "mode": "approved"})
        await http.post("/v1/press", json={"key": "Control+Enter", "mode": "approved", "fingerprint": PRINT})
        await http.post("/v1/select", json={"ref": "e3", "fingerprint": PRINT, "values": ["a", "b"], "mode": "approved"})
        shot = await http.post("/v1/screenshot", json={"full_page": True})
        assert shot.headers["content-type"] == "image/png" and shot.content == PNG
        assert shot.headers["cache-control"] == "no-store"
        assert (await http.get("/v1/downloads")).json() == [{"name": "report.csv", "size": 3, "finished": True}]
    assert fake.calls[: len(ACTIONS)] == [
        ("navigate", "https://example.com/", False), ("snapshot", 100), ("describe", "e1", False),
        ("click", "e1", PRINT, "safe"), ("type", "e1", PRINT, "hello", True, False, "safe"),
        ("press", "Enter", "safe", None), ("select", "e1", PRINT, ["a"], "safe"), ("scroll", "down", 1),
        ("history", False), ("history", True), ("activate", "t1"), ("close", "t1"), ("screenshot", False),
        ("upload", "e1", PRINT, "a.txt", "0" * 64), ("downloads",),
    ]
    assert fake.calls[len(ACTIONS):] == [
        ("navigate", "https://example.com/x", True), ("describe", None, True),
        ("type", "e2", PRINT, "", False, True, "approved"), ("press", "Control+Enter", "approved", PRINT),
        ("select", "e3", PRINT, ["a", "b"], "approved"), ("screenshot", True), ("downloads",),
    ]


BAD = [
    ("/v1/navigate", {}, 400), ("/v1/navigate", {"url": ""}, 400), ("/v1/navigate", {"url": "   "}, 400),
    ("/v1/navigate", {"url": 7}, 400), ("/v1/navigate", {"url": ["https://example.com/"]}, 400),
    ("/v1/navigate", {"url": "https://example.com/" + "a" * 2100}, 400),
    ("/v1/navigate", {"url": "https://example.com/", "new_tab": "yes"}, 400),
    ("/v1/snapshot", {"max_chars": -1}, 400), ("/v1/snapshot", {"max_chars": 20001}, 400),
    ("/v1/snapshot", {"max_chars": "100"}, 400), ("/v1/snapshot", {"max_chars": True}, 400),
    ("/v1/snapshot", {"max_chars": 1.5}, 400),
    ("/v1/describe", {}, 404), ("/v1/describe", {"ref": "1"}, 404), ("/v1/describe", {"ref": 'e1"] *'}, 404),
    ("/v1/describe", {"focused": "true"}, 404),
    ("/v1/click", {"fingerprint": PRINT}, 404), ("/v1/click", {"ref": "e1"}, 400),
    ("/v1/click", {"ref": "e1", "fingerprint": "short"}, 400), ("/v1/click", {"ref": "e1", "fingerprint": 5}, 400),
    ("/v1/click", {"ref": "e1", "fingerprint": "f" * 200}, 400),
    ("/v1/click", {"ref": "e1", "fingerprint": PRINT, "mode": "user"}, 400),
    ("/v1/click", {"ref": "e1", "fingerprint": PRINT, "mode": "APPROVED"}, 400),
    ("/v1/click", {"ref": "e1", "fingerprint": PRINT, "mode": None}, 400),
    ("/v1/click", {"ref": "../e1", "fingerprint": PRINT}, 404),
    ("/v1/type", {"ref": "e1", "fingerprint": PRINT}, 400),
    ("/v1/type", {"ref": "e1", "fingerprint": PRINT, "text": 5}, 400),
    ("/v1/type", {"ref": "e1", "fingerprint": PRINT, "text": "x" * 5001}, 400),
    ("/v1/type", {"ref": "e1", "fingerprint": PRINT, "text": "x", "clear": "no"}, 400),
    ("/v1/type", {"ref": "e1", "fingerprint": PRINT, "text": "x", "submit": 1}, 400),
    ("/v1/press", {}, 400), ("/v1/press", {"key": "F12"}, 400), ("/v1/press", {"key": "Control+L"}, 400),
    ("/v1/press", {"key": "enter"}, 400), ("/v1/press", {"key": ["Enter"]}, 400),
    ("/v1/press", {"key": "Enter", "mode": "root"}, 400), ("/v1/press", {"key": "Enter", "fingerprint": 5}, 400),
    ("/v1/select", {"ref": "e1", "fingerprint": PRINT}, 400),
    ("/v1/select", {"ref": "e1", "fingerprint": PRINT, "values": []}, 400),
    ("/v1/select", {"ref": "e1", "fingerprint": PRINT, "values": "a"}, 400),
    ("/v1/select", {"ref": "e1", "fingerprint": PRINT, "values": [1]}, 400),
    ("/v1/select", {"ref": "e1", "fingerprint": PRINT, "values": ["a"] * 21}, 400),
    ("/v1/select", {"ref": "e1", "fingerprint": PRINT, "values": ["x" * 201]}, 400),
    ("/v1/scroll", {}, 400), ("/v1/scroll", {"direction": "left"}, 400),
    ("/v1/scroll", {"direction": "down", "pages": 0}, 400), ("/v1/scroll", {"direction": "down", "pages": 11}, 400),
    ("/v1/scroll", {"direction": "down", "pages": True}, 400), ("/v1/scroll", {"direction": "down", "pages": "2"}, 400),
    ("/v1/screenshot", {"full_page": "yes"}, 400),
    ("/v1/upload", {"fingerprint": PRINT, "path": "a.txt"}, 404), ("/v1/upload", {"ref": "e1", "path": "a.txt"}, 400),
    ("/v1/tabs/1/activate", None, 404), ("/v1/tabs/t1x/close", None, 404), ("/v1/tabs/T1/close", None, 404),
    ("/v1/tabs/t123456789/activate", None, 404),
]


@pytest.mark.parametrize(("path", "body", "status"), BAD)
async def test_wrong_input_never_reaches_the_browser(path, body, status):
    app, fake = make()
    async with client(app) as http:
        answer = await send(http, "POST", path, body)
        assert answer.status_code == status, answer.text
        assert set(answer.json()) == {"error"}
    assert fake.calls == []


async def test_bodies_that_are_not_json_objects_are_refused():
    app, fake = make()
    async with client(app) as http:
        for content in (b"not json", b"[1, 2]", b'"text"', b"\xff\xfe", b"{", b"null"):
            answer = await http.post("/v1/click", content=content)
            assert answer.status_code in {400, 404}, content
    assert fake.calls == []


async def test_only_approved_mode_may_press_send_shortcuts():
    """The route passes the key on; the session refuses Control+Enter unless approved."""
    real = session.Session(settings.Settings(token=TOKEN, headless=True))
    for key in ("Control+Enter", "Meta+Enter"):
        with pytest.raises(BrowserdError) as refused:
            await real.press(key, "safe")
        assert (refused.value.code, refused.value.status) == ("bad_key", 403)
    with pytest.raises(BrowserdError) as refused:
        await real.press("F5", "approved")
    assert refused.value.code == "bad_key"


# --- how failures look ---

async def test_refusals_carry_a_short_code_and_nothing_else():
    app, fake = make()
    async with client(app) as http:
        for code, status in (("element_changed", 409), ("sensitive_field", 403), ("no_such_element", 404),
                             ("timeout", 504), ("unavailable", 503), ("too_many_tabs", 400)):
            fake.fail = BrowserdError(code, status)
            answer = await http.post("/v1/click", json=ACTIONS[3][2])
            assert (answer.status_code, answer.json()) == (status, {"error": code})
        fake.fail = RuntimeError("secret detail: https://bank.example/?token=abc")
        answer = await http.post("/v1/click", json=ACTIONS[3][2])
        assert (answer.status_code, answer.json()) == (500, {"error": "failed"})
        assert "bank.example" not in answer.text


async def test_the_log_says_which_route_and_never_what_was_sent(capsys):
    app, fake = make()
    async with client(app) as http:
        await http.post("/v1/navigate", json={"url": "https://private.example/inbox?id=42"})
        await http.post("/v1/type", json={"ref": "e1", "fingerprint": PRINT, "text": "a personal note"})
        await http.get("/healthz")
        fake.fail = RuntimeError("boom at https://private.example/")
        await http.post("/v1/back")
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    assert [(line["route"], line["status"]) for line in lines] == [
        ("/v1/navigate", 200), ("/v1/type", 200), ("/v1/back", 500),
    ]
    assert all(set(line) == {"ts", "route", "status", "ms"} for line in lines)
    logged = json.dumps(lines)
    for private in ("private.example", "personal note", TOKEN, PRINT, "boom"):
        assert private not in logged


async def test_broken_text_from_a_page_cannot_break_the_answer():
    app, fake = make()
    fake.page = {"ok": True, "mode": "agent", "url": "https://example.com/", "title": "half \ud800 pair",
                 "elements": [{"ref": "e1", "name": "\udc00", "options": ["\ud83d"]}]}
    async with client(app) as http:
        answer = await http.post("/v1/snapshot", json={"max_chars": 10})
        assert answer.status_code == 200
        assert answer.json()["title"] == "half ? pair" and answer.json()["elements"][0]["name"] == "?"


# --- settings ---

@pytest.fixture
def env(monkeypatch):
    for name in list(os.environ):
        if name.startswith(("BROWSER", "VNC_")) or name in {"DISPLAY", "TZ"}:
            monkeypatch.delenv(name)
    return monkeypatch


def test_settings_defaults_match_the_spec(env):
    env.setenv("BROWSER_API_TOKEN", TOKEN)
    config = settings.Settings.from_env()
    assert (config.host, config.port, config.peers) == ("10.77.4.40", 7100, frozenset({"10.77.4.10"}))
    assert (config.profile_dir, config.files_dir) == (Path("/profile"), Path("/files"))
    assert (config.downloads_dir, config.uploads_dir) == (Path("/files/downloads"), Path("/files/uploads"))
    assert (config.display, config.viewport, config.max_tabs) == (":99", (1280, 800), 2)
    assert (config.action_timeout_s, config.nav_timeout_s) == (30, 45)
    assert config.chromium_sandbox is False and config.headless is False
    assert config.allow_private_hosts == frozenset() and config.block_background_posts is False
    assert config.sensitive_match == "word" and config.timezone == "Europe/Helsinki"
    assert TOKEN not in repr(config)


def test_token_comes_from_the_secret_file_first(env, tmp_path):
    secret = tmp_path / "browser_api_token"
    secret.write_text(f"{TOKEN}\n")
    env.setenv("BROWSER_API_TOKEN_FILE", str(secret))
    env.setenv("BROWSER_API_TOKEN", "from-the-environment")
    assert settings.Settings.from_env().token == TOKEN
    env.setenv("BROWSER_API_TOKEN_FILE", str(tmp_path / "missing"))
    with pytest.raises(SystemExit) as stopped:
        settings.Settings.from_env()
    assert str(tmp_path) not in str(stopped.value) and "from-the-environment" not in str(stopped.value)


@pytest.mark.parametrize(("name", "value"), [
    ("BROWSER_API_TOKEN", ""), ("BROWSERD_ALLOWED_PEERS", "*"), ("BROWSERD_ALLOWED_PEERS", "10.77.4.10, *"),
    ("BROWSER_VIEWPORT", "big"), ("BROWSER_VIEWPORT", "100x100"), ("BROWSER_VIEWPORT", "99999x800"),
    ("BROWSER_MAX_TABS", "0"), ("BROWSER_MAX_TABS", "50"), ("BROWSER_MAX_TABS", "two"),
    ("BROWSER_SENSITIVE_MATCH", "regex"), ("BROWSER_CHROMIUM_SANDBOX", "maybe"),
    ("BROWSER_BLOCK_BACKGROUND_POSTS", "2"), ("BROWSERD_PORT", "0"), ("BROWSER_NAV_TIMEOUT_S", "-1"),
])
def test_settings_that_make_no_sense_stop_the_service(env, name, value):
    env.setenv("BROWSER_API_TOKEN", TOKEN)
    env.setenv(name, value)
    with pytest.raises(SystemExit):
        settings.Settings.from_env()


def test_settings_read_the_switches(env):
    env.setenv("BROWSER_API_TOKEN", TOKEN)
    env.setenv("BROWSER_ALLOW_PRIVATE_HOSTS", " Fixture-Web , 127.0.0.1,")
    env.setenv("BROWSER_SENSITIVE_MATCH", "Substring")
    env.setenv("BROWSER_BLOCK_BACKGROUND_POSTS", "true")
    env.setenv("BROWSER_CHROMIUM_SANDBOX", "1")
    env.setenv("BROWSER_VIEWPORT", "1024x768")
    env.setenv("BROWSER_MAX_TABS", "3")
    config = settings.Settings.from_env()
    assert config.allow_private_hosts == frozenset({"fixture-web", "127.0.0.1"})
    assert (config.sensitive_match, config.block_background_posts, config.chromium_sandbox) == ("substring", True, True)
    assert (config.viewport, config.max_tabs) == ((1024, 768), 3)


# --- what the page reports is checked before it is used ---

RAW = {
    "ref": "e7", "tag": "INPUT", "role": "", "name": "Card", "type": "TEXT", "href": "", "value": "4111 1111",
    "aria_label": "", "title_attr": "", "in_form": True, "form_method": "POST",
    "form_action": "https://shop.example/pay", "form_submit_name": "Pay", "submits": False, "disabled": False,
    "sensitive": True, "aria_expanded": None, "aria_haspopup": False, "contenteditable": False,
    "inside_dialog_title": "",
}


def test_a_secret_fields_value_is_dropped_whatever_the_page_script_said():
    element = session.sane_element(RAW)
    assert element["sensitive"] is True and element["value"] == ""
    assert (element["tag"], element["type"], element["form_method"]) == ("input", "text", "post")
    password = session.sane_element({**RAW, "sensitive": False, "type": "password", "value": "hunter2"})
    assert password["sensitive"] is True and password["value"] == ""
    assert "hunter2" not in json.dumps(password) and "4111" not in json.dumps(element)
    plain = session.sane_element({**RAW, "sensitive": False, "name": "City", "value": "Helsinki"})
    assert plain["value"] == "Helsinki" and plain["sensitive"] is False


def test_page_data_is_cut_typed_and_stripped_of_extras():
    hostile = {
        **RAW, "sensitive": False, "name": "n" * 5000, "value": "v" * 5000, "href": "h" * 9000,
        "tag": {"x": 1}, "role": ["button"], "in_form": "yes", "submits": 1, "disabled": "false",
        "aria_expanded": "true", "inside_dialog_title": "t" * 500, "fingerprint": "page-made",
        "cookies": "sid=1", "__proto__": {"x": 1}, "checked": "yes", "options": ["a" * 500, 5, "b"] + ["c"] * 50,
        "more_options": -3, "ref": "e7\"] , body",
    }
    element = session.sane_element(hostile)
    assert set(element) == {*guards.FINGERPRINT_KEYS, "ref", "fingerprint", "options", "more_options"}
    assert len(element["name"]) == 200 and len(element["value"]) == 200 and len(element["href"]) == 2048
    assert (element["tag"], element["role"], element["ref"]) == ("", "", "")
    assert element["in_form"] is False and element["submits"] is False and element["disabled"] is False
    assert element["aria_expanded"] is None and len(element["inside_dialog_title"]) == 80
    assert len(element["options"]) <= 20 and all(len(option) <= 60 for option in element["options"])
    assert element["more_options"] == 0
    assert element["fingerprint"] == guards.fingerprint(element) != "page-made"
    for junk in (None, "text", 5, ["e1"], True):
        assert session.sane_element(junk) is None and session.sane_structure(junk) is None


def test_the_fingerprint_is_made_here_from_the_checked_facts():
    one = session.sane_element(RAW)
    assert one["fingerprint"] == session.sane_element({**RAW, "fingerprint": "0" * 64})["fingerprint"]
    assert one["fingerprint"] != session.sane_element({**RAW, "form_action": "https://evil.example/pay"})["fingerprint"]
    assert one["fingerprint"] != session.sane_element({**RAW, "name": "Card number"})["fingerprint"]


def test_headings_and_regions_carry_no_ref():
    assert session.sane_structure({"role": "Heading", "name": "Your cart", "level": 1, "ref": "e1", "href": "x"}) == {
        "role": "heading", "name": "Your cart", "level": 1,
    }
    assert session.sane_structure({"role": "", "name": 5, "level": 9}) == {"role": "region", "name": ""}


# --- the profile and staged uploads ---

def profile_session(tmp_path) -> session.Session:
    config = settings.Settings(token=TOKEN, profile_dir=tmp_path / "profile", files_dir=tmp_path / "files", headless=True)
    return session.Session(config)


def test_profile_is_prepared_before_every_start(tmp_path):
    browser = profile_session(tmp_path)
    profile = tmp_path / "profile"
    (profile / "Default").mkdir(parents=True)
    (profile / "Default" / "Preferences").write_text(json.dumps({
        "profile": {"name": "Roland", "password_manager_enabled": True}, "custom": {"kept": 1},
    }))
    (profile / "SingletonLock").symlink_to("old-container-1234")  # what a killed Chromium leaves
    (profile / "SingletonCookie").write_text("x")
    incoming = tmp_path / "files" / ".incoming"
    incoming.mkdir(parents=True)
    (incoming / "half-a-download").write_bytes(b"partial")
    (tmp_path / "files" / "downloads").mkdir()
    (tmp_path / "files" / "downloads" / "kept.csv").write_text("a,b")

    browser.prepare_profile()

    assert not (profile / "SingletonLock").is_symlink() and not (profile / "SingletonCookie").exists()
    assert list(incoming.iterdir()) == []
    assert (tmp_path / "files" / "downloads" / "kept.csv").read_text() == "a,b"
    assert (tmp_path / "files" / "uploads").is_dir()
    saved = json.loads((profile / "Default" / "Preferences").read_text())
    assert saved["profile"] == {"name": "Roland", "password_manager_enabled": False,
                                "password_manager_leak_detection": False}
    assert saved["custom"] == {"kept": 1} and saved["credentials_enable_service"] is False
    assert saved["autofill"]["credit_card_enabled"] is False and saved["signin"]["allowed"] is False
    assert saved["download"] == {"prompt_for_download": False,
                                 "default_directory": str(tmp_path / "files" / "downloads")}
    assert session.PROFILE_PREFERENCES["download"] == {"prompt_for_download": False}  # the template is untouched
    browser.prepare_profile()  # and again, on the next start


def test_a_preferences_file_that_cannot_be_read_is_left_alone(tmp_path):
    browser = profile_session(tmp_path)
    target = tmp_path / "profile" / "Default" / "Preferences"
    target.parent.mkdir(parents=True)
    target.write_text("{not json")
    browser.prepare_profile()
    assert target.read_text() == "{not json"


def test_chromium_gets_a_small_environment_without_the_token(tmp_path, monkeypatch):
    monkeypatch.setenv("BROWSER_API_TOKEN", TOKEN)
    monkeypatch.setenv("BROWSER_API_TOKEN_FILE", "/run/secrets/browser_api_token")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    env = profile_session(tmp_path)._chromium_env()
    assert set(env) == {"HOME", "PATH", "LANG", "TZ", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"}
    assert TOKEN not in json.dumps(env) and "secrets" not in json.dumps(env)
    headed = session.Session(settings.Settings(token=TOKEN, display=":42"))
    assert headed._chromium_env()["DISPLAY"] == ":42"


def test_staged_uploads_are_read_without_following_links(tmp_path):
    folder = tmp_path / "uploads"
    folder.mkdir()
    (folder / "note.txt").write_bytes(b"for the website")
    (tmp_path / "secret.txt").write_text("not for upload")
    (folder / "link.txt").symlink_to(tmp_path / "secret.txt")
    (folder / "folder").mkdir()
    assert session.read_upload(folder, "note.txt") == b"for the website"
    for name in ("link.txt", "folder", "missing.txt", "../secret.txt", ".hidden", "", "a/b", None):
        with pytest.raises(BrowserdError) as refused:
            session.read_upload(folder, name)
        assert (refused.value.code, refused.value.status) == ("no_such_file", 404), name


def test_staged_uploads_have_a_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "MAX_UPLOAD_BYTES", 10)
    (tmp_path / "ok.bin").write_bytes(b"x" * 10)
    (tmp_path / "big.bin").write_bytes(b"x" * 11)
    assert len(session.read_upload(tmp_path, "ok.bin")) == 10
    with pytest.raises(BrowserdError):
        session.read_upload(tmp_path, "big.bin")


# --- the POST guard's decision ---

class Request:
    def __init__(self, method="POST", resource_type="document"):
        self.method, self.resource_type = method, resource_type


def guard(tmp_path, **changes) -> session.Session:
    return session.Session(settings.Settings(token=TOKEN, headless=True, **changes))


def test_form_submissions_are_stopped_unless_roland_approved_or_is_driving(tmp_path):
    browser = guard(tmp_path)
    for method in ("POST", "post", "PUT", "DELETE", "PATCH"):
        assert browser._stops_post(Request(method)) is True
    for method in ("GET", "HEAD", "get", "OPTIONS"):
        assert browser._stops_post(Request(method)) is False
    # Background requests are let through by default (the classifier in core gates what starts them) ...
    for kind in ("fetch", "xhr", "ping", "image", "script", "other"):
        assert browser._stops_post(Request("POST", kind)) is False
    # ... and an approved action opens the door only while it runs.
    import time

    browser._posts_open_until = time.monotonic() + 5
    assert browser._stops_post(Request("POST")) is False
    browser._posts_open_until = time.monotonic() - 0.01
    assert browser._stops_post(Request("POST")) is True
    browser.mode = "user"
    assert browser._stops_post(Request("POST")) is False


def test_the_stricter_switch_also_stops_background_posts(tmp_path):
    browser = guard(tmp_path, block_background_posts=True)
    for kind in ("fetch", "xhr", "ping", "other"):
        assert browser._stops_post(Request("POST", kind)) is True
        assert browser._stops_post(Request("GET", kind)) is False
    assert browser._stops_post(Request("POST", "image")) is False


# --- the launcher ---

def test_processes_that_die_are_started_again_more_slowly_each_time():
    assert [launcher.backoff(n) for n in range(8)] == [0, 2, 4, 8, 16, 30, 30, 30]
    assert launcher.MAX_BACKOFF_S == 30


def test_the_screen_takes_no_network_connections_and_m7_adds_nothing_yet():
    child = launcher.xvfb_child(":99", 1280, 800)
    assert child.argv[:5] == ["Xvfb", ":99", "-screen", "0", "1280x800x24"]
    assert child.argv[child.argv.index("-nolisten") + 1] == "tcp"
    assert launcher.extra_children(":99") == []
    assert launcher.screen_socket(":99") == Path("/tmp/.X11-unix/X99")
