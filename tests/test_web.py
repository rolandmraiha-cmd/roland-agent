import time

import pytest
from fastapi.testclient import TestClient

from agent.web import auth
from agent.web.app import create_app

PW = "correct horse battery staple"
ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture
def client(make_agent):
    agent = make_agent(["Hi Roland, how can I help"] * 5)
    with TestClient(create_app(agent, run_scheduler=False)) as c:
        c.agent = agent
        yield c


def login(c, pw=PW):
    r = c.post("/login", data={"password": pw}, headers=ORIGIN, follow_redirects=False)
    if r.status_code == 303:
        c.headers["X-CSRF-Token"] = c.get("/api/status").json()["csrf"]
    return r


def test_pages_need_login(client):
    assert client.get("/", follow_redirects=False).status_code == 303
    assert client.get("/api/chats").status_code == 401
    assert client.get("/login").status_code == 200


def test_login_sets_safe_cookie_and_logout(client):
    r = login(client)
    assert r.status_code == 303
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/api/chats").status_code == 200
    assert client.post("/logout", headers=ORIGIN).status_code == 200
    assert client.get("/api/chats").status_code == 401


def test_wrong_password_and_lockout(client, monkeypatch):
    monkeypatch.setattr("agent.web.app.asyncio.sleep", _no_sleep)
    for _ in range(auth.PER_IP_FAILS):
        assert login(client, "wrong").status_code == 401
    r = login(client)  # even the right password is refused while locked
    assert r.status_code == 429 and "Too many" in r.text


def test_limiter_global_lock_and_expiry():
    lim = auth.LoginLimiter()
    now = time.time()
    for i in range(auth.GLOBAL_FAILS):
        lim.failed(f"10.0.0.{i}", now)
    assert lim.locked("1.2.3.4", now) > 0
    assert lim.locked("1.2.3.4", now + auth.LOCKOUT + 1) == 0


def test_csrf_token_required(client):
    login(client)
    good = client.headers.pop("X-CSRF-Token")
    assert client.post("/api/chats", headers=ORIGIN).status_code == 403
    assert client.post("/api/chats", headers={**ORIGIN, "X-CSRF-Token": "x" * 64}).status_code == 403
    assert client.post("/api/chats", headers={**ORIGIN, "X-CSRF-Token": good}).status_code == 200


def test_csrf_origin_required(client):
    login(client)
    assert client.post("/api/chats").status_code == 403
    assert client.post("/api/chats", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/chats", headers=ORIGIN).status_code == 200


def test_security_headers(client):
    r = client.get("/login")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"


def test_chat_stream_and_history(client):
    login(client)
    chat_id = client.post("/api/chats", headers=ORIGIN).json()["id"]
    with client.stream("POST", f"/api/chats/{chat_id}/send", json={"text": "hello"}, headers=ORIGIN) as r:
        body = "".join(r.iter_text())
    assert r.headers["content-type"].startswith("text/event-stream")
    assert body.count('"type": "text"') >= 3 and '"type": "end"' in body
    msgs = client.get(f"/api/chats/{chat_id}/messages").json()["messages"]
    assert msgs[0]["content"] == "hello" and msgs[1]["content"].startswith("Hi Roland")
    assert client.get("/api/chats").json()[0]["title"] == "hello"


def test_jobs_api(client):
    login(client)
    assert client.post("/api/jobs", json={"name": "n", "cron": "nope nope", "prompt": "p"}, headers=ORIGIN).status_code in (400, 422)
    job_id = client.post("/api/jobs", json={"name": "News", "cron": "0 7 * * *", "prompt": "news"}, headers=ORIGIN).json()["id"]
    assert client.post(f"/api/jobs/{job_id}/toggle", headers=ORIGIN).json() == {"enabled": False}
    client.post(f"/api/jobs/{job_id}/run", headers=ORIGIN)
    for _ in range(50):
        runs = client.get("/api/jobs").json()["runs"]
        if runs and runs[0]["finished"]:
            break
        time.sleep(0.05)
    assert runs[0]["ok"] == 1 and "Hi Roland" in runs[0]["output"]
    assert client.get("/api/jobs").json()["jobs"][0]["enabled"] is False  # run-now doesn't unpause
    assert client.delete(f"/api/jobs/{job_id}", headers=ORIGIN).status_code == 200


async def _no_sleep(_):
    return None


def test_password_hashing():
    h = auth.hash_password("a long password here")
    assert h.startswith("$argon2id$") and "a long password" not in h
    assert auth.password_ok("a long password here", h)
    assert not auth.password_ok("wrong", h)
    assert not auth.password_ok("a long password here", "not-a-hash")
    assert not auth.password_ok("", h)


def test_trusted_hosts(make_agent):
    agent = make_agent(allowed_hosts=("agent.example.com",))
    with TestClient(create_app(agent, run_scheduler=False)) as c:
        assert c.get("/login").status_code == 400
        assert c.get("/login", headers={"Host": "agent.example.com"}).status_code == 200
