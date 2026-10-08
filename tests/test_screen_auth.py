"""A7.1: screen sessions and /internal/screen-auth (§6.6). Caddy asks core before it lets any
request through to noVNC; these tests play Caddy against a fake browserd."""

from __future__ import annotations

import logging
import time

import pytest
from fastapi.testclient import TestClient
from screen_helpers import CADDY, HOST, ORIGIN, VNC_FULL, VNC_VIEW, ScreenBrowserd, login, screen_agent

from agent.browser_client import BrowserLocked
from agent.web.app import create_app
from agent.web.auth import cookie_name


@pytest.fixture
def fake():
    return ScreenBrowserd()


def caddy(agent, peer: str = CADDY) -> TestClient:
    return TestClient(create_app(agent, run_scheduler=False), base_url=ORIGIN, client=(peer, 1))


def auth(client, kind: str = "static", **headers) -> int:
    """What Caddy's forward_auth gets back for one visitor request."""
    response = client.get(f"/internal/screen-auth?kind={kind}", headers=headers)
    assert response.content == b""
    return response.status_code


def start(client, mode: str = "watch", **extra) -> dict:
    response = client.post("/api/screen/session", json={"mode": mode, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def test_screen_auth_requires_session(tmp_path, fake):
    agent = screen_agent(tmp_path, fake)
    with caddy(agent) as client:
        assert auth(client) == 401 and auth(client, "ws", Origin=ORIGIN) == 401
        client.cookies.set(cookie_name(agent.config), "synthetic-invalid-token")
        assert auth(client) == 401
        client.cookies.clear()
        login(client)
        start(client)
        assert auth(client) == 200
        # A login that has ended is no login, even with a screen session still on record.
        token = client.cookies.get(cookie_name(agent.config))
        agent.memory.delete_all_sessions()
        client.cookies.set(cookie_name(agent.config), token)
        assert auth(client) == 401


def test_screen_auth_requires_active_screen_session(tmp_path, fake):
    agent = screen_agent(tmp_path, fake)
    with caddy(agent) as client:
        login(client)
        assert auth(client) == 403 and auth(client, "ws", Origin=ORIGIN) == 403
        session = start(client)
        assert auth(client) == 200 and auth(client, "ws", Origin=ORIGIN) == 200
        assert client.post("/api/screen/release", json={"id": session["id"]}).json() == {
            "ok": True,
            "ended": True,
        }
        assert auth(client) == 403
        # Idle past SCREEN_SESSION_IDLE_MIN without a heartbeat: over, and the page is told.
        session = start(client)
        agent.memory._exec(
            "UPDATE screen_sessions SET last_seen = ? WHERE id = ?", (time.time() - 31 * 60, session["id"])
        )
        assert auth(client) == 403
        assert client.post("/api/screen/heartbeat", json={"id": session["id"]}).status_code == 410
        # A heartbeat keeps a session alive, and only its own login may send it.
        session = start(client)
        agent.memory._exec(
            "UPDATE screen_sessions SET last_seen = ? WHERE id = ?", (time.time() - 29 * 60, session["id"])
        )
        beat = client.post("/api/screen/heartbeat", json={"id": session["id"]})
        assert beat.status_code == 200 and beat.json()["expires"] > time.time() + 29 * 60
        assert auth(client) == 200
        assert "kind" not in beat.json() and "vnc_password" not in beat.json()
        assert auth(client, "other") == 403


def test_ws_origin_must_match(tmp_path, fake):
    agent = screen_agent(tmp_path, fake)
    with caddy(agent) as client:
        login(client)
        start(client)
        client.headers.pop("Origin")
        assert auth(client, "ws", Origin=ORIGIN) == 200
        for origin in (
            "https://evil.test",
            f"http://{HOST}",
            f"https://{HOST}.evil.test",
            f"{ORIGIN}/path",
            "null",
            "",
        ):
            assert auth(client, "ws", Origin=origin) == 403, origin
        assert auth(client, "ws") == 403  # no Origin at all
        # Static noVNC files are plain GETs and carry no Origin.
        assert auth(client, "static") == 200


def test_screen_auth_peer_must_be_caddy(tmp_path, fake):
    agent = screen_agent(tmp_path, fake, core_allowed_peers=(CADDY, "10.77.4.40"))
    with caddy(agent) as client:
        login(client)
        start(client)
        assert auth(client) == 200
        cookie = client.cookies.get(cookie_name(agent.config))
    # The browser container and the sandbox hold no say here, with or without Roland's cookie.
    for peer in ("10.77.4.40", "10.77.3.20", "127.0.0.1"):
        with caddy(agent, peer) as other:
            other.cookies.set(cookie_name(agent.config), cookie)
            response = other.get("/internal/screen-auth?kind=static", headers={"X-Forwarded-For": CADDY})
            assert response.status_code == 403, peer
    with caddy(agent) as client:
        client.cookies.set(cookie_name(agent.config), cookie)
        assert client.post("/internal/screen-auth?kind=static").status_code == 403
        assert client.get("/internal/other").status_code == 404


def test_only_one_screen_session(tmp_path, fake):
    agent = screen_agent(tmp_path, fake)
    with caddy(agent) as phone, caddy(agent) as laptop:
        login(phone)
        login(laptop)
        first = start(phone, "control")
        assert fake.mode == "user"
        before = fake.disconnects
        second = start(laptop, "watch")
        assert first["id"] != second["id"]
        assert [row["id"] for row in agent.memory.screen_sessions()] == [second["id"]]
        # The first page is cut off and Roland's control ends with it.
        assert fake.disconnects == before + 1 and fake.mode == "agent"
        assert auth(phone) == 403 and auth(laptop) == 200
        assert phone.post("/api/screen/heartbeat", json={"id": first["id"]}).status_code == 410
        # One login can't end or keep alive another login's session.
        assert phone.post("/api/screen/release", json={"id": second["id"]}).json()["ended"] is False
        assert phone.post("/api/screen/heartbeat", json={"id": second["id"]}).status_code == 410
        assert auth(laptop) == 200
        ends = agent.memory.audit_rows(event="screen_session_end")
        assert [row["detail"]["reason"] for row in ends] == ["replaced"]


def test_logout_ends_screen_and_disconnects(tmp_path, fake):
    agent = screen_agent(tmp_path, fake)
    with caddy(agent) as client:
        login(client)
        start(client, "control")
        assert fake.mode == "user" and agent.screens.roland_has_browser()
        before = fake.disconnects
        assert client.post("/logout").status_code == 200
        assert fake.disconnects == before + 1  # the fake browserd recorded the disconnect
        assert fake.mode == "agent" and not agent.screens.roland_has_browser()
        assert agent.memory.screen_sessions() == []
        assert auth(client) == 401
        end = agent.memory.audit_rows(event="screen_session_end")[0]
        assert end["detail"]["reason"] == "logout" and end["detail"]["mode"] == "control"
        # Order matters: the screen is cut before the agent gets the browser back.
        tail = [path for path in fake.paths("/v1/") if path != "/v1/status"][-2:]
        assert tail == ["/v1/vnc/disconnect", "/v1/user-mode"]


def test_vnc_password_not_in_logs_or_audit(tmp_path, fake, caplog):
    caplog.set_level(logging.DEBUG)
    agent = screen_agent(tmp_path, fake)
    with caddy(agent) as client:
        login(client)
        page = client.get("/screen")
        assert page.status_code == 200 and page.headers["Cache-Control"] == "no-store"
        watch = start(client, "watch")
        control = start(client, "control")
        # Watching gets the view-only password; only taking control gets the full one.
        assert watch["vnc_password"] == VNC_VIEW and control["vnc_password"] == VNC_FULL
        assert watch["ws_path"] == control["ws_path"] == "/screen/websockify"
        client.post("/api/screen/heartbeat", json={"id": control["id"]})
        status = client.get("/api/status")
        client.post("/api/screen/release", json={"id": control["id"]})
        client.post("/logout")
        static = "".join(client.get(f"/static/{name}").text for name in ("screen.js", "app.js", "style.css"))
    for secret in (VNC_FULL, VNC_VIEW):
        assert secret not in caplog.text
        assert secret not in page.text and secret not in static and secret not in status.text
        assert secret not in repr(agent.config) and secret not in repr(agent.ctx)
        for table in ("audit_log", "screen_sessions", "messages", "signin_requests"):
            rows = agent.memory._all(f"SELECT * FROM {table}")  # noqa: S608 -- fixed table names
            assert all(secret not in str(tuple(row)) for row in rows), table
    # What the audit does say: who, when, which mode, how long. Nothing typed, nothing seen.
    events = [
        (row["event"], row["detail"])
        for row in reversed(agent.memory.audit_rows(limit=50))
        if row["event"].startswith("screen_")
    ]
    assert [event for event, _ in events] == ["screen_session_start", "screen_session_end"] * 2
    assert all(set(detail) <= {"mode", "duration_s", "reason"} for _, detail in events)
    # Even if a password did reach an audit detail by mistake, it would be blanked out.
    agent.audit.write("system", "config_warning", detail={"note": f"oops {VNC_FULL} and {VNC_VIEW}"})
    assert (
        agent.memory.audit_rows(event="config_warning")[0]["detail"]["note"]
        == "oops [redacted] and [redacted]"
    )


def test_screen_routes_do_not_exist_while_the_screen_is_off(tmp_path, fake):
    for settings in (dict(screen_enabled=False), dict(browser_enabled=False)):
        agent = screen_agent(tmp_path / str(len(settings)) / str(settings), fake, **settings)
        with caddy(agent) as client:
            login(client)
            assert client.get("/screen").status_code == 404
            assert client.post("/api/screen/session", json={"mode": "watch"}).status_code == 404
            assert client.post("/api/screen/heartbeat", json={"id": "x"}).status_code == 404
            assert client.post("/api/screen/release", json={"id": "x"}).status_code == 404
            assert auth(client) == 403 and auth(client, "ws", Origin=ORIGIN) == 403
            assert client.get("/api/status").json()["screen"] == {"enabled": False}
            assert not agent.screens.roland_has_browser()
    assert fake.paths("/v1/user-mode") == [] and fake.disconnects == 0


def test_bad_session_requests_are_refused(tmp_path, fake):
    agent = screen_agent(tmp_path, fake)
    with caddy(agent) as client:
        login(client)
        for body in (
            {},
            {"mode": "admin"},
            {"mode": "watch", "signin_id": ""},
            {"mode": "watch", "signin_id": "x" * 65},
        ):
            assert client.post("/api/screen/session", json=body).status_code == 422, body
        assert (
            client.post("/api/screen/session", json={"mode": "control", "signin_id": "nope"}).status_code
            == 409
        )
        assert agent.memory.screen_sessions() == []
        # Without the CSRF token nothing starts, and the page itself needs the login.
        token = client.headers.pop("X-CSRF-Token")
        assert client.post("/api/screen/session", json={"mode": "watch"}).status_code == 403
        client.headers["X-CSRF-Token"] = token
        client.cookies.clear()
        assert client.get("/screen", follow_redirects=False).status_code == 303
        assert client.post("/api/screen/session", json={"mode": "watch"}).status_code in {401, 403}


def test_control_is_refused_when_the_browser_cannot_be_handed_over(tmp_path, fake):
    agent = screen_agent(tmp_path, fake)
    with caddy(agent) as client:
        login(client)
        fake.unreachable = True
        response = client.post("/api/screen/session", json={"mode": "control"})
        assert response.status_code == 503 and "vnc_password" not in response.text
        assert agent.memory.screen_sessions() == [] and not agent.screens.roland_has_browser()
        assert agent.memory.audit_rows(event="screen_session_end")[0]["detail"]["reason"] == "failed"


@pytest.mark.asyncio
async def test_the_agent_is_locked_out_while_roland_has_control(tmp_path, fake):
    """Core refuses the agent's own browser calls from the moment control starts, without
    asking browserd: a browserd that restarted has forgotten user mode."""
    agent = screen_agent(tmp_path, fake)
    browser = agent.ctx.browser
    login_hash = "a" * 64
    await agent.screens.start(login_hash, "watch")
    assert (await browser.snapshot(2000))["title"] == "Sign in"  # watching leaves the agent working
    session = await agent.screens.start(login_hash, "control")
    fake.mode = "agent"  # browserd lost it
    before = len(fake.calls)
    for attempt in (
        browser.snapshot(2000),
        browser.screenshot(),
        browser.navigate("https://example.com/"),
        browser.describe("e1"),
        browser.click("e1", "f" * 16, "safe"),
    ):
        with pytest.raises(BrowserLocked, match="Roland is using the browser right now"):
            await attempt
    assert len(fake.calls) == before  # nothing reached browserd
    assert (await browser.status())["mode"] == "agent"  # status stays readable for the Browser tab
    # Housekeeping notices the disagreement and puts browserd right again.
    await agent.screens.tick()
    assert fake.mode == "user"
    assert await agent.screens.release(session["id"], login_hash)
    assert fake.mode == "agent" and (await browser.snapshot(2000))["title"] == "Sign in"


@pytest.mark.asyncio
async def test_restart_ends_sessions_and_housekeeping_cleans_up(tmp_path, fake):
    agent = screen_agent(tmp_path, fake)
    await agent.screens.start("b" * 64, "control")
    assert fake.mode == "user"
    # A new core process on the same database: the old page's session means nothing now.
    again = screen_agent(tmp_path, fake)
    fake.unreachable = True  # browserd isn't up yet; starting must not wait for it
    assert again.screens.reset_on_startup() == 1
    assert again.memory.screen_sessions() == [] and not again.screens.roland_has_browser()
    await again.screens.tick()
    assert fake.mode == "user"
    fake.unreachable = False
    before = fake.disconnects
    await again.screens.tick()
    assert fake.disconnects == before + 1 and fake.mode == "agent"
    await again.screens.tick()
    assert fake.disconnects == before + 1  # once is enough
    assert again.memory.audit_rows(event="screen_session_end")[0]["detail"]["reason"] == "restart"
    # With nothing open and nothing owed, housekeeping leaves browserd alone.
    quiet = len(fake.calls)
    for _ in range(3):
        await again.screens.tick()
    assert len(fake.calls) == quiet


@pytest.mark.asyncio
async def test_housekeeping_retries_a_hand_back_browserd_missed(tmp_path, fake):
    """Roland releases the screen while browserd can't be reached: the records say the agent
    has the browser, browserd still says Roland. The next rounds put that right, then stop."""
    agent = screen_agent(tmp_path, fake)
    login_hash = "d" * 64
    session = await agent.screens.start(login_hash, "control")
    assert fake.mode == "user"
    fake.unreachable = True
    assert await agent.screens.release(session["id"], login_hash)
    assert not agent.screens.roland_has_browser() and fake.mode == "user"
    await agent.screens.tick()  # still unreachable: tried, nothing changed
    assert fake.mode == "user"
    fake.unreachable = False
    before = fake.disconnects
    await agent.screens.tick()
    assert fake.mode == "agent" and fake.disconnects == before + 1
    quiet = len(fake.calls)
    await agent.screens.tick()
    assert len(fake.calls) == quiet


@pytest.mark.asyncio
async def test_housekeeping_retries_when_only_the_mode_switch_was_missed(tmp_path, fake):
    """The screen was cut, but browserd did not take "the agent has the browser again".
    Until it does, the agent's calls come back locked; housekeeping keeps at it."""
    agent = screen_agent(tmp_path, fake)
    login_hash = "e" * 64
    session = await agent.screens.start(login_hash, "control")
    fake.down_paths = {"/v1/user-mode"}
    before = fake.disconnects
    assert await agent.screens.release(session["id"], login_hash)
    assert fake.disconnects == before + 1 and fake.mode == "user"
    await agent.screens.tick()  # still failing
    assert fake.mode == "user"
    fake.down_paths = set()
    await agent.screens.tick()
    assert fake.mode == "agent" and fake.disconnects == before + 1  # no second cut was owed
    quiet = len(fake.calls)
    await agent.screens.tick()
    assert len(fake.calls) == quiet
