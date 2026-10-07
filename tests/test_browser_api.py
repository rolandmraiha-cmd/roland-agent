"""Browser tab routes use fake browserd responses, never a real browser."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from agent.web import routes_browser
from agent.web.app import create_app

PASSWORD = "correct horse battery staple"
ORIGIN = "https://agent.test"
TOKEN = "synthetic"
PNG = b"\x89PNG\r\n\x1a\nfixture thumbnail"


class FakeBrowserd:
    def __init__(self):
        self.answer = {
            "mode": "agent",
            "url": "https://fixture.example/blog",
            "title": "Fixture blog",
            "tabs": [
                {"id": "t1", "url": "https://fixture.example/blog", "title": "Fixture blog", "active": True}
            ],
        }
        self.image = PNG
        self.status_code = 200
        self.screenshot_code = 200
        self.unreachable = False
        self.hang = False
        self.cancelled = 0
        self.calls: list[tuple[str, str, dict]] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, request.url.path, body))
        if self.unreachable:
            raise httpx.ConnectError("fake browser is down", request=request)
        if request.url.path == "/v1/status":
            if self.hang:
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    self.cancelled += 1
                    raise
            return httpx.Response(self.status_code, json=self.answer)
        assert request.url.path == "/v1/screenshot"
        if self.screenshot_code != 200:
            return httpx.Response(
                self.screenshot_code,
                json={"error": "user_mode" if self.screenshot_code == 423 else "timeout"},
            )
        return httpx.Response(200, content=self.image, headers={"Content-Type": "image/png"})


def login(client) -> None:
    response = client.post(
        "/login",
        data={"password": PASSWORD},
        headers={"Origin": ORIGIN},
        follow_redirects=False,
    )
    assert response.status_code == 303
    client.headers["X-CSRF-Token"] = client.get("/api/status").json()["csrf"]
    client.headers["Origin"] = ORIGIN


@pytest.fixture
def browser_env(make_agent):
    agent = make_agent(browser_enabled=True, browser_api_token=TOKEN)
    fake = FakeBrowserd()
    agent.ctx.browser.transport = httpx.MockTransport(fake.handle)
    with TestClient(create_app(agent, run_scheduler=False), base_url=ORIGIN) as client:
        login(client)
        fake.calls.clear()
        yield client, agent, fake


def test_browser_routes_require_session(make_agent):
    agent = make_agent(browser_enabled=True, browser_api_token=TOKEN)
    fake = FakeBrowserd()
    agent.ctx.browser.transport = httpx.MockTransport(fake.handle)
    with TestClient(create_app(agent, run_scheduler=False), base_url=ORIGIN) as client:
        assert client.get("/api/browser/status").status_code == 401
        assert client.post("/api/browser/screenshot", headers={"Origin": ORIGIN}).status_code == 401
    assert fake.calls == []


def test_screenshot_requires_csrf_and_origin(browser_env):
    client, _, fake = browser_env
    token = client.headers.pop("X-CSRF-Token")
    assert client.post("/api/browser/screenshot").status_code == 403
    client.headers["X-CSRF-Token"] = token
    assert client.post("/api/browser/screenshot", headers={"Origin": "https://other.test"}).status_code == 403
    client.headers.pop("Origin")
    assert client.post("/api/browser/screenshot").status_code == 403
    assert fake.calls == []


def test_browser_off(make_agent):
    agent = make_agent()
    with TestClient(create_app(agent, run_scheduler=False), base_url=ORIGIN) as client:
        login(client)
        assert client.get("/api/browser/status").json() == {"enabled": False}
        assert client.post("/api/browser/screenshot").status_code == 404
        assert client.get("/api/status").json()["browser"] == {"enabled": False, "mode": None, "url": None}


def test_browser_status_and_global_summary(browser_env):
    client, _, fake = browser_env
    assert client.get("/api/browser/status").json() == {"enabled": True, "reachable": True, **fake.answer}
    assert client.get("/api/status").json()["browser"] == {
        "enabled": True,
        "mode": "agent",
        "url": fake.answer["url"],
    }
    fake.answer["mode"] = "user"
    assert client.get("/api/browser/status").json()["mode"] == "user"
    assert client.get("/api/status").json()["browser"]["mode"] == "user"


@pytest.mark.parametrize("failure", ["unreachable", "http_error", "malformed"])
def test_browser_down_keeps_global_status_available(browser_env, failure):
    client, _, fake = browser_env
    if failure == "unreachable":
        fake.unreachable = True
    elif failure == "http_error":
        fake.status_code = 503
    else:
        fake.answer = ["not a status object"]
    assert client.get("/api/browser/status").json() == {"enabled": True, "reachable": False}
    response = client.get("/api/status")
    assert response.status_code == 200
    assert response.json()["browser"] == {"enabled": True, "mode": None, "url": None}
    assert "csrf" in response.json()


def test_untrusted_status_fields_are_bounded_and_typed(browser_env):
    client, _, fake = browser_env
    fake.answer = {
        "mode": ["user"],
        "url": "u" * 1000,
        "title": "t" * 1000,
        "tabs": [{"id": "i" * 1000, "url": "u" * 1000, "title": "t" * 1000, "active": "true"}] * 30,
        "unexpected": "private extra data",
    }
    result = client.get("/api/browser/status").json()
    assert result["mode"] is None
    assert result["url"] == "u" * 300
    assert result["title"] == "t" * 120
    assert len(result["tabs"]) == 20
    assert result["tabs"][0] == {"id": "i" * 120, "url": "u" * 300, "title": "t" * 120, "active": False}
    assert "unexpected" not in result
    assert client.get("/api/status").json()["browser"]["url"] == "u" * 300

    fake.answer = {
        "mode": "unknown",
        "url": {"bad": "url"},
        "title": 1,
        "tabs": [None, "bad", {"id": 1, "url": [], "title": False, "active": 1}],
    }
    result = client.get("/api/browser/status").json()
    assert result["mode"] is None and result["url"] is None and result["title"] is None
    assert result["tabs"] == [{"id": None, "url": None, "title": None, "active": False}]
    fake.answer["tabs"] = {"bad": "tabs"}
    assert client.get("/api/browser/status").json()["tabs"] == []


def test_screenshot_is_png_thumbnail_without_agent_side_effects(browser_env, monkeypatch):
    client, agent, fake = browser_env
    before_audit = agent.memory.audit_rows()
    before_files = list(agent.config.workspace.rglob("*"))

    def forbidden(*args, **kwargs):
        pytest.fail("browser UI must not call the tool, audit, model or workspace paths")

    monkeypatch.setattr(agent.audit, "write", forbidden)
    monkeypatch.setattr(agent.brain, "stream", forbidden)
    monkeypatch.setattr("agent.tools.call_tool", forbidden)
    monkeypatch.setattr("agent.workspace.Workspace.write_bytes", forbidden)
    assert client.get("/api/browser/status").status_code == 200
    response = client.post("/api/browser/screenshot")
    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert fake.calls == [("GET", "/v1/status", {}), ("POST", "/v1/screenshot", {"full_page": False})]
    assert agent.memory.audit_rows() == before_audit
    assert list(agent.config.workspace.rglob("*")) == before_files
    assert agent.brain.seen == []


@pytest.mark.parametrize(
    "failure,expected", [("locked", 423), ("unreachable", 503), ("http_error", 503), ("not_png", 503)]
)
def test_screenshot_failures_are_json(browser_env, failure, expected):
    client, _, fake = browser_env
    if failure == "locked":
        fake.screenshot_code = 423
    elif failure == "unreachable":
        fake.unreachable = True
    elif failure == "http_error":
        fake.screenshot_code = 503
    else:
        fake.image = b"<html>not a PNG</html>"
    response = client.post("/api/browser/screenshot")
    assert response.status_code == expected
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-store"
    assert "error" in response.json()


def test_status_deadline_cancels_slow_transport(browser_env, monkeypatch):
    client, _, fake = browser_env
    monkeypatch.setattr(routes_browser, "STATUS_TIMEOUT_S", 0.02)
    fake.hang = True
    start = time.monotonic()
    assert client.get("/api/browser/status").json() == {"enabled": True, "reachable": False}
    assert client.get("/api/status").json()["browser"] == {"enabled": True, "mode": None, "url": None}
    assert time.monotonic() - start < 1
    assert fake.cancelled == 2
