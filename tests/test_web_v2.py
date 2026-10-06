import asyncio
import re
import time
from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path

import pytest
from conftest import HASH
from fastapi import WebSocket
from fastapi.testclient import TestClient
from httpx2 import Headers
from starlette.routing import Mount
from starlette.websockets import WebSocketDisconnect

from agent.config import Config, core_peer_networks
from agent.web.app import create_app
from agent.web.auth import COOKIE, Sessions, cookie_name
from agent.web.middleware import DIRECT_PEER, AuthMiddleware, PeerAllowlist, ProxyHeaders, SecurityHeaders

PASSWORD = "correct horse battery staple"


def login(client, origin="https://agent.test"):
    response = client.post(
        "/login", data={"password": PASSWORD}, headers={"Origin": origin}, follow_redirects=False
    )
    assert response.status_code == 303
    client.headers["X-CSRF-Token"] = client.get("/api/status").json()["csrf"]
    return response


def socket_app(agent, path="/ws"):
    app = create_app(agent, run_scheduler=False)
    entered = []

    @app.websocket(path)
    async def socket(websocket: WebSocket):
        entered.append(True)
        await websocket.accept()
        await websocket.send_json({"ok": True})
        await websocket.close()

    return app, entered


def test_every_route_requires_auth(make_agent):
    app = create_app(make_agent(), run_scheduler=False)
    with TestClient(app) as client:
        checked = 0
        for route in app.routes:
            if isinstance(route, Mount):
                assert route.path == "/static"
                continue
            if route.path in {"/login", "/healthz", "/favicon.ico"}:
                continue
            path = re.sub(r"\{[^}]+\}", "1", route.path)
            for method in route.methods:
                response = client.request(method, path, follow_redirects=False)
                if path.startswith("/internal"):
                    assert response.status_code == 403
                else:
                    assert response.status_code in {401, 303}, (method, path, response.status_code)
                checked += 1
        assert checked >= 15
        assert client.get("/static/style.css").status_code == 200
        assert client.get("/static/app.js").status_code == 200


@pytest.mark.parametrize(
    "token,origin,code",
    [
        ("missing", "https://agent.test", 4401),
        ("invalid", "https://agent.test", 4401),
        ("valid", "https://evil.test", 4403),
        ("valid", "http://agent.test", 4403),
        ("valid", "", 4403),
        ("valid", "null", 4403),
        ("valid", "https://agent.test/path", 4403),
    ],
)
def test_websocket_requires_session_and_origin(make_agent, token, origin, code):
    agent = make_agent(cookie_secure=True, agent_host="agent.test")
    app, entered = socket_app(agent)
    with TestClient(app, base_url="https://agent.test") as client:
        login(client)
        headers = {"Origin": origin}
        if token == "valid":
            headers["Cookie"] = f"{cookie_name(agent.config)}={client.cookies.get(cookie_name(agent.config))}"
        elif token == "invalid":
            headers["Cookie"] = f"{cookie_name(agent.config)}=synthetic-invalid-token"
        client.cookies.clear()
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect("wss://agent.test/ws", headers=headers):
                pass
        assert error.value.code == code
    assert entered == []


def test_websocket_accepts_matching_cookie_and_origin(make_agent):
    agent = make_agent(cookie_secure=True, agent_host="agent.test")
    app, entered = socket_app(agent)
    with TestClient(app, base_url="https://agent.test") as client:
        login(client)
        headers = {
            "Origin": "https://agent.test",
            "Cookie": f"{cookie_name(agent.config)}={client.cookies.get(cookie_name(agent.config))}",
        }
        with client.websocket_connect("wss://agent.test/ws", headers=headers) as socket:
            assert socket.receive_json() == {"ok": True}
    assert entered == [True]


def test_websocket_public_http_path_is_still_protected(make_agent):
    app, entered = socket_app(make_agent(), path="/login")
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect("/login", headers={"Origin": "http://testserver"}):
                pass
        assert error.value.code == 4401
    assert entered == []


def test_websocket_duplicate_origin_is_denied(make_agent):
    agent = make_agent(cookie_secure=True, agent_host="agent.test")
    app, entered = socket_app(agent)
    with TestClient(app, base_url="https://agent.test") as client:
        login(client)
        headers = Headers(
            [
                ("Origin", "https://agent.test"),
                ("Origin", "https://evil.test"),
                ("Cookie", f"{cookie_name(agent.config)}={client.cookies.get(cookie_name(agent.config))}"),
            ]
        )
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect("wss://agent.test/ws", headers=headers):
                pass
        assert error.value.code == 4403
    assert entered == []


def test_websocket_logged_out_cookie_is_refused(make_agent):
    agent = make_agent(cookie_secure=True, agent_host="agent.test")
    app, entered = socket_app(agent)
    with TestClient(app, base_url="https://agent.test") as client:
        login(client)
        cookie = f"{cookie_name(agent.config)}={client.cookies.get(cookie_name(agent.config))}"
        assert client.post("/logout", headers={"Origin": "https://agent.test"}).status_code == 200
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect(
                "wss://agent.test/ws", headers={"Origin": "https://agent.test", "Cookie": cookie}
            ):
                pass
        assert error.value.code == 4401
    assert not entered


@pytest.mark.parametrize("limit", ["expiry", "idle"])
def test_websocket_expired_session_is_refused(make_agent, monkeypatch, limit):
    agent = make_agent(
        cookie_secure=True,
        agent_host="agent.test",
        idle_hours=24 * 30 if limit == "expiry" else 72,
    )
    app, entered = socket_app(agent)
    with TestClient(app, base_url="https://agent.test") as client:
        login(client)
        cookie = f"{cookie_name(agent.config)}={client.cookies.get(cookie_name(agent.config))}"
        duration = agent.config.session_days * 86400 if limit == "expiry" else agent.config.idle_hours * 3600
        future = time.time() + duration + 1
        monkeypatch.setattr("agent.web.auth.time.time", lambda: future)
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect(
                "wss://agent.test/ws", headers={"Origin": "https://agent.test", "Cookie": cookie}
            ):
                pass
        assert error.value.code == 4401
    assert not entered


@pytest.mark.parametrize(
    "peer,expected",
    [
        ("10.77.1.2", 200),
        ("127.0.0.1", 200),
        ("::1", 200),
        ("::ffff:127.0.0.1", 200),
        ("10.77.3.20", 403),
        ("testclient", 403),
    ],
)
def test_peer_allowlist_blocks_unknown_peer(make_agent, peer, expected):
    agent = make_agent(core_allowed_peers=("10.77.1.2",), trusted_proxies=("10.77.1.2",))
    with TestClient(create_app(agent, run_scheduler=False), client=(peer, 1)) as client:
        response = client.get("/login", headers={"X-Forwarded-For": "10.77.1.2"})
        assert response.status_code == expected
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["Cross-Origin-Opener-Policy"] == "same-origin"


def test_peer_controls_run_before_forwarded_address_and_auth(make_agent, monkeypatch):
    monkeypatch.setattr("agent.web.app.asyncio.sleep", _no_sleep)
    agent = make_agent(core_allowed_peers=("10.77.1.2",), trusted_proxies=("10.77.1.2",))
    app = create_app(agent, run_scheduler=False)
    with TestClient(app, client=("10.77.1.2", 1)) as client:
        assert client.get("/login", headers={"X-Forwarded-For": "8.8.8.8"}).status_code == 200
        response = client.post(
            "/login",
            data={"password": "wrong"},
            headers={"Origin": "http://testserver", "X-Forwarded-For": "8.8.8.8"},
        )
        assert response.status_code == 401
        row = agent.memory._all("SELECT detail FROM audit_log WHERE event='login_fail'")[0]
        assert '"client":"8.8.8.8"' in row["detail"]
    with TestClient(app, client=("10.77.3.20", 1)) as client:
        assert client.get("/healthz", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 403


def test_bad_host_and_peer_refusals_keep_security_headers(make_agent):
    agent = make_agent(agent_host="agent.test", core_allowed_peers=("10.77.1.2",))
    app = create_app(agent, run_scheduler=False)
    for peer, expected in [("10.77.1.2", 400), ("10.77.3.20", 403)]:
        with TestClient(app, client=(peer, 1)) as client:
            response = client.get("/login", headers={"Host": "evil.test"}, follow_redirects=False)
            assert response.status_code == expected
            assert response.headers["X-Frame-Options"] == "DENY"
            assert "unsafe-inline" not in response.headers["Content-Security-Policy"]


async def _no_sleep(delay):
    pass


def test_healthz_is_minimal_and_self_check_has_no_extra_access(make_agent):
    agent = make_agent(host="10.77.1.10", core_allowed_peers=("10.77.1.2",))
    app = create_app(agent, run_scheduler=False)
    with TestClient(app, client=("10.77.1.10", 1)) as client:
        assert client.get("/healthz").json() == {"ok": True}
        assert client.get("/api/status").status_code == 403
        assert client.get("/login").status_code == 403
        assert client.post("/healthz").status_code == 403
        assert client.get("/internal/screen-auth").status_code == 403
    with TestClient(app, client=("10.77.4.40", 1)) as client:
        assert client.get("/healthz").status_code == 403


def test_internal_routes_only_use_the_original_explicit_proxy_peer(make_agent):
    agent = make_agent(core_allowed_peers=("10.77.1.2", "10.77.4.40"), trusted_proxies=("10.77.1.2",))
    app = create_app(agent, run_scheduler=False)
    with TestClient(app, client=("10.77.1.2", 1)) as client:
        assert client.get("/internal/unknown", headers={"X-Forwarded-For": "8.8.8.8"}).status_code == 404
        assert client.get("/internal/screen-auth", headers={"X-Forwarded-For": "8.8.8.8"}).status_code == 403
        assert client.post("/internal/unknown").status_code == 404
    for peer in ("127.0.0.1", "10.77.4.40"):
        with TestClient(app, client=(peer, 1)) as client:
            assert (
                client.get("/internal/unknown", headers={"X-Forwarded-For": "10.77.1.2"}).status_code == 403
            )
            assert client.post("/internal/screen-auth").status_code == 403


def test_websocket_peer_is_denied_before_route_accept(make_agent):
    agent = make_agent(core_allowed_peers=("10.77.1.2",))
    app, entered = socket_app(agent)
    with TestClient(app, client=("10.77.4.40", 1)) as client:
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect("/ws", headers={"X-Forwarded-For": "10.77.1.2"}):
                pass
        assert error.value.code == 4403
    assert entered == []


def test_host_cookie_prefix_when_secure(make_agent):
    agent = make_agent(cookie_secure=True, agent_host="agent.test")
    with TestClient(create_app(agent, run_scheduler=False), base_url="https://agent.test") as client:
        response = login(client)
        header = response.headers["set-cookie"]
        assert header.startswith("__Host-agent_session=")
        assert "Secure" in header and "HttpOnly" in header and "SameSite=strict" in header
        assert "Path=/" in header and "Domain=" not in header
        assert client.cookies.get(COOKIE) is None
        token = client.cookies.get(cookie_name(agent.config))
        assert client.post("/api/chats", headers={"Origin": "https://agent.test"}).status_code == 200
        response = client.post("/logout", headers={"Origin": "https://agent.test"})
        assert response.status_code == 200
        deleted = response.headers["set-cookie"]
        assert deleted.startswith("__Host-agent_session=") and "Secure" in deleted and "Max-Age=0" in deleted
        client.cookies.set(COOKIE, token)
        assert client.get("/api/status").status_code == 401
    assert cookie_name(Config(cookie_secure=False)) == COOKIE


@pytest.mark.parametrize(
    "origin,csrf",
    [
        ("https://evil.test", "valid"),
        ("http://agent.test", "valid"),
        ("https://[", "valid"),
        ("null", "valid"),
        ("https://agent.test", "wrong"),
        ("https://agent.test", "å"),
    ],
)
def test_secure_cookie_csrf_and_origin_checks(make_agent, origin, csrf):
    agent = make_agent(cookie_secure=True, agent_host="agent.test")
    with TestClient(create_app(agent, run_scheduler=False), base_url="https://agent.test") as client:
        login(client)
        headers = [(b"origin", origin.encode("utf-8"))]
        if csrf != "valid":
            headers.append((b"x-csrf-token", csrf.encode("utf-8")))
        before = agent.memory.chats()
        assert client.post("/api/chats", headers=headers).status_code == 403
        assert agent.memory.chats() == before


def test_duplicate_origin_and_csrf_headers_do_not_fall_back(make_agent):
    agent = make_agent(cookie_secure=True, agent_host="agent.test")
    with TestClient(create_app(agent, run_scheduler=False), base_url="https://agent.test") as client:
        login(client)
        headers = [("Origin", "https://agent.test"), ("Origin", "null"), ("Referer", "https://agent.test/")]
        assert client.post("/api/chats", headers=headers).status_code == 403
        token = client.headers.pop("X-CSRF-Token")
        headers = [("Origin", "https://agent.test"), ("X-CSRF-Token", token), ("X-CSRF-Token", token)]
        assert client.post("/api/chats", headers=headers).status_code == 403
        assert (
            client.post(
                "/api/chats", headers={"Referer": "https://agent.test/page", "X-CSRF-Token": token}
            ).status_code
            == 200
        )


def test_csp_has_no_unsafe_inline(make_agent):
    agent = make_agent(agent_host="agent.test")
    with TestClient(create_app(agent, run_scheduler=False), base_url="http://agent.test") as client:
        for path in ("/login", "/static/app.js", "/api/status", "/missing"):
            response = client.get(path, follow_redirects=False)
            policy = response.headers["Content-Security-Policy"]
            assert "unsafe-inline" not in policy and "unsafe-eval" not in policy
            assert "script-src 'self'" in policy and "style-src 'self'" in policy
            assert "connect-src 'self' wss://agent.test" in policy
            assert "img-src 'self' data: blob:" in policy
            assert response.headers["Cross-Origin-Resource-Policy"] == "same-origin"
            assert "clipboard-read=()" in response.headers["Permissions-Policy"]
        assert client.get("/api/status").headers["Cache-Control"] == "no-store"


class PageParser(HTMLParser):
    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        assert tag != "style"
        assert "style" not in values and not any(key.startswith("on") for key in values)
        if tag == "script":
            assert values.get("src", "").startswith("/static/")


def test_core_pages_have_no_inline_scripts_styles_or_handlers():
    folder = Path(__file__).resolve().parents[1] / "agent/web/static"
    for path in folder.glob("*.html"):
        PageParser().feed(path.read_text())


def test_unhandled_errors_keep_security_headers_and_hide_details(make_agent):
    app = create_app(make_agent(), run_scheduler=False)

    @app.get("/api/test-failure")
    async def failure():
        raise RuntimeError("Synthetic private details")

    with TestClient(app, raise_server_exceptions=False) as client:
        login(client, "http://testserver")
        response = client.get("/api/test-failure")
        assert response.status_code == 500 and response.json() == {"error": "internal server error"}
        assert "Synthetic private details" not in response.text
        assert response.headers["X-Frame-Options"] == "DENY"
        assert "unsafe-inline" not in response.headers["Content-Security-Policy"]


@pytest.mark.parametrize("entry", ["*", "0.0.0.0/0", "::/0", "not-an-IP"])
def test_invalid_peer_lists_are_refused(entry):
    config = Config(password_hash=HASH, model_base_url="http://127.0.0.1:8080", core_allowed_peers=(entry,))
    with pytest.raises(SystemExit, match="CORE_ALLOWED_PEERS"):
        config.check()
    with pytest.raises(ValueError, match="CORE_ALLOWED_PEERS"):
        core_peer_networks((entry,))


@pytest.mark.parametrize(
    "host",
    [
        "https://agent.test",
        "agent.test:443",
        "agent.test/path",
        "agent.test; script-src *",
        "agent.test\r\nX-Injected: yes",
        "",
        "agent.test",
    ],
)
def test_served_hostname_is_checked_before_csp(make_agent, host):
    agent = make_agent(agent_host=host)
    if host in {"", "agent.test"}:
        create_app(agent, run_scheduler=False)
        agent.config.check()
    else:
        with pytest.raises(ValueError, match="AGENT_HOST"):
            create_app(agent, run_scheduler=False)
        with pytest.raises(SystemExit, match="AGENT_HOST"):
            agent.config.check()


def test_production_peer_default_cannot_disable_the_filter():
    config = Config(
        agent_env="production",
        password_hash=HASH,
        agent_host="agent.test",
        model_server_token="test-token",
        model_base_url="http://127.0.0.1:8080",
    )
    assert config.core_allowed_peers == ("10.77.1.2",)
    assert replace(config, core_allowed_peers=()).core_allowed_peers == ("10.77.1.2",)
    config.check()
    assert Config().core_allowed_peers == ()


def test_lifespan_passes_through_all_asgi_controls(make_agent):
    agent = make_agent()
    reached = []

    async def terminal(scope, receive, send):
        reached.append(scope["type"])

    middleware = AuthMiddleware(
        terminal,
        agent.config,
        Sessions(agent.memory, 14, 72, HASH),
    )
    middleware = ProxyHeaders(middleware, agent.config.trusted_proxies)
    middleware = PeerAllowlist(middleware, agent.config)
    middleware = SecurityHeaders(middleware, agent.config)
    asyncio.run(middleware({"type": "lifespan"}, None, None))
    assert reached == ["lifespan"]


async def test_proxy_preserves_original_peer_and_sets_websocket_scheme():
    seen = []

    async def terminal(scope, receive, send):
        seen.append(scope)

    middleware = ProxyHeaders(terminal, ("10.77.1.2",))
    await middleware(
        {
            "type": "websocket",
            "client": ("10.77.1.2", 1),
            "scheme": "ws",
            "headers": [(b"x-forwarded-for", b"8.8.8.8"), (b"x-forwarded-proto", b"https")],
        },
        None,
        None,
    )
    assert seen[0][DIRECT_PEER] == ("10.77.1.2", 1)
    assert seen[0]["client"] == ("8.8.8.8", 1) and seen[0]["scheme"] == "wss"
