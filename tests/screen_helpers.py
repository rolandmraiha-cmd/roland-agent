"""Shared by the M7 tests: a browserd stand-in that knows user mode and screen connections,
and an agent with the screen switched on. No real browser, VNC server or network."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import httpx
from conftest import FakeBrain, make_config

from agent.core import Agent
from agent.memory import Memory
from agent.web.app import create_app

PASSWORD = "correct horse battery staple"
HOST = "agent.test"
ORIGIN = f"https://{HOST}"
CADDY = "10.77.1.2"
TOKEN = "browser-test-token"
# Shaped like `make secrets` writes them: eight characters, and different from each other.
VNC_FULL = "Fu11pw9Z"
VNC_VIEW = "V1ewpw7Q"
PNG = b"\x89PNG\r\n\x1a\nfixture"
OPEN_IN_USER_MODE = {"/healthz", "/v1/status", "/v1/user-mode", "/v1/vnc/disconnect"}


class ScreenBrowserd:
    """browserd as core sees it for M7: who has the browser, and screen connections cut."""

    def __init__(self):
        self.mode = "agent"
        self.url = "https://shop.example/"
        self.disconnects = 0
        self.unreachable = False
        self.down_paths: set[str] = set()   # only these calls fail to connect
        self.forget_mode_on_status = False  # a browserd that restarted and lost user mode
        self.disconnect_delay = 0.0         # the real one restarts x11vnc, which takes a moment
        self.calls: list[tuple[str, str, dict]] = []

    def paths(self, prefix: str = "") -> list[str]:
        return [path for _, path, _ in self.calls if path.startswith(prefix)]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle_slowly)

    async def handle_slowly(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/vnc/disconnect" and self.disconnect_delay:
            await asyncio.sleep(self.disconnect_delay)
        return self.handle(request)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, path, body))
        if self.unreachable or path in self.down_paths:
            raise httpx.ConnectError("fake browser is down", request=request)
        assert request.headers.get("authorization") == f"Bearer {TOKEN}"
        if self.mode == "user" and path not in OPEN_IN_USER_MODE:
            return httpx.Response(423, json={"error": "user_mode"})
        if path == "/v1/user-mode":
            self.mode = "user" if body["on"] is True else "agent"
            return httpx.Response(200, json={"mode": self.mode})
        if path == "/v1/vnc/disconnect":
            self.disconnects += 1
            return httpx.Response(200, json={"ok": True})
        if path == "/v1/status":
            return httpx.Response(200, json={
                "mode": self.mode, "url": self.url, "title": "Shop",
                "tabs": [{"id": "t1", "url": self.url, "title": "Shop", "active": True}],
            })
        if path == "/v1/navigate":
            if "10.0.0.5" in body["url"]:
                return httpx.Response(200, json={"url": self.url, "title": "Shop", "status": 0, "blocked": "private_address"})
            self.url = body["url"]
            return httpx.Response(200, json={"url": self.url, "title": "Sign in", "status": 200})
        if path == "/v1/snapshot":
            return httpx.Response(200, json={
                "url": self.url, "title": "Sign in", "text": "Welcome back", "truncated": False,
                "login_form_detected": True,
                "elements": [{"ref": "e1", "tag": "input", "type": "password", "name": "Password", "sensitive": True}],
            })
        if path == "/v1/screenshot":
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        return httpx.Response(404, json={"error": "not_found"})


def screen_agent(tmp_path, fake: ScreenBrowserd, script=(), **kw) -> Agent:
    """An agent behind Caddy with the browser and the screen switched on."""
    settings = dict(
        browser_enabled=True, browser_api_token=TOKEN, screen_enabled=True,
        vnc_password=VNC_FULL, vnc_view_password=VNC_VIEW, agent_host=HOST, cookie_secure=True,
        core_allowed_peers=(CADDY,), trusted_proxies=(CADDY,),
    )
    settings.update(kw)
    config = make_config(tmp_path, **settings)
    agent = Agent(config, Memory(config.db_path), FakeBrain(script))
    if agent.ctx.browser is not None:
        agent.ctx.browser.transport = fake.transport()
    return agent


def login(client) -> None:
    response = client.post("/login", data={"password": PASSWORD}, headers={"Origin": ORIGIN}, follow_redirects=False)
    assert response.status_code == 303, response.text
    client.headers["Origin"] = ORIGIN
    client.headers["X-CSRF-Token"] = client.get("/api/status").json()["csrf"]


@asynccontextmanager
async def web(agent: Agent, peer: str = CADDY):
    """The web app on this test's own event loop, so a chat that is waiting for Roland and
    the requests that answer it can run side by side. Logged in, as Caddy would deliver it."""
    app = create_app(agent, run_scheduler=False)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=(peer, 1))
        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
            response = await client.post("/login", data={"password": PASSWORD}, headers={"Origin": ORIGIN})
            assert response.status_code == 303, response.text
            client.headers["Origin"] = ORIGIN
            client.headers["X-CSRF-Token"] = (await client.get("/api/status")).json()["csrf"]
            yield client
