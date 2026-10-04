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


def test_limiter_never_locks_out_other_addresses():
    lim = auth.LoginLimiter()
    now = time.time()
    for i in range(auth.GLOBAL_FAILS):
        lim.failed(f"10.0.0.{i}", now)
    assert lim.locked("1.2.3.4", now) == 0          # Roland's address can still log in
    assert lim.slowdown(now) == auth.GLOBAL_SLOWDOWN  # but wrong guesses are slowed down
    assert lim.slowdown(now + auth.WINDOW + 1) == 0
    for _ in range(auth.PER_IP_FAILS):
        lim.failed("6.6.6.6", now)
    assert lim.locked("6.6.6.6", now) > 0
    assert lim.locked("6.6.6.6", now + auth.LOCKOUT + 1) == 0


def test_lockout_is_per_address(make_agent, monkeypatch):
    monkeypatch.setattr("agent.web.app.asyncio.sleep", _no_sleep)
    agent = make_agent()
    with TestClient(create_app(agent, run_scheduler=False), client=("6.6.6.6", 1)) as bad:
        for _ in range(auth.PER_IP_FAILS):
            login(bad, "wrong")
        assert login(bad).status_code == 429
        app = bad.app
    with TestClient(app, client=("1.2.3.4", 1)) as good:
        assert login(good).status_code == 303


def test_idle_session_expires(client, monkeypatch):
    login(client)
    assert client.get("/api/chats").status_code == 200
    later = time.time() + client.agent.config.idle_hours * 3600 + 120
    monkeypatch.setattr("agent.web.auth.time.time", lambda: later)
    assert client.get("/api/chats").status_code == 401


def test_password_change_ends_sessions(tmp_path, make_agent):
    from agent.memory import Memory
    agent = make_agent()
    s1 = auth.Sessions(agent.memory, 14, 72, agent.config.password_hash)
    token = s1.create()
    assert s1.valid(token)
    s2 = auth.Sessions(agent.memory, 14, 72, auth.hash_password("a brand new password"))
    assert not s2.valid(token)
    s3 = auth.Sessions(agent.memory, 14, 72, auth.hash_password("a brand new password"))
    t3 = s3.create()
    assert s3.valid(t3)


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
    assert client.get("/api/jobs").json()["jobs"][0]["approved"] is True  # jobs Roland adds need no OK
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


def test_agent_made_jobs_wait_for_approval(client):
    login(client)
    mem = client.agent.memory
    job_id = mem.add_job("Sneaky", "* * * * *", "do something", 0, approved=False)
    assert mem.due_jobs(time.time() + 3600) == []
    job = client.get("/api/jobs").json()["jobs"][0]
    assert job["approved"] is False and job["enabled"] is False
    assert client.post(f"/api/jobs/{job_id}/toggle", headers=ORIGIN).status_code == 400
    assert client.post(f"/api/jobs/{job_id}/run", headers=ORIGIN).status_code == 400
    assert client.post(f"/api/jobs/{job_id}/approve", headers=ORIGIN).status_code == 200
    job = client.get("/api/jobs").json()["jobs"][0]
    assert job["approved"] is True and job["enabled"] is True


def test_run_now_limits(client):
    from agent.scheduler import running_jobs
    login(client)
    mem = client.agent.memory
    a = mem.add_job("A", "0 7 * * *", "a", 0)
    b = mem.add_job("B", "0 7 * * *", "b", 0)
    c = mem.add_job("C", "0 7 * * *", "c", 0)
    running_jobs.update({a, b})
    try:
        assert client.post(f"/api/jobs/{a}/run", headers=ORIGIN).status_code == 409
        assert client.post(f"/api/jobs/{c}/run", headers=ORIGIN).status_code == 429
    finally:
        running_jobs.difference_update({a, b})


def test_facts_listed_and_deleted(client):
    login(client)
    fid = client.agent.memory.remember("Roland likes tea")
    facts = client.get("/api/jobs").json()["facts"]
    assert facts == [{"id": fid, "text": "Roland likes tea"}]
    assert client.delete(f"/api/facts/{fid}", headers=ORIGIN).status_code == 200
    assert client.get("/api/jobs").json()["facts"] == []
    assert client.delete(f"/api/facts/{fid}", headers=ORIGIN).status_code == 404


def test_origin_scheme_checked_when_secure(make_agent):
    agent = make_agent(cookie_secure=True)
    with TestClient(create_app(agent, run_scheduler=False), base_url="https://testserver") as c:
        r = c.post("/login", data={"password": PW}, headers={"Origin": "http://testserver"},
                   follow_redirects=False)
        assert r.status_code == 403


def test_ipv6_counted_per_64():
    assert auth.client_key("2001:db8:1:2:aaaa::1") == auth.client_key("2001:db8:1:2:bbbb::9")
    assert auth.client_key("2001:db8:1:2::1") != auth.client_key("2001:db8:1:3::1")
    assert auth.client_key("::ffff:1.2.3.4") == "1.2.3.4"
    assert auth.client_key("1.2.3.4") == "1.2.3.4"


def test_wrong_guesses_dont_hold_up_roland(make_agent, monkeypatch):
    """The slowdown sleep happens outside the login lock: while attackers' wrong guesses sleep,
    Roland's login still goes straight through."""
    import asyncio
    import httpx
    real_sleep = asyncio.sleep

    async def slow_sleep(seconds):
        await real_sleep(1.5)  # every wrong guess "sleeps" 1.5 s

    monkeypatch.setattr("agent.web.app.asyncio.sleep", slow_sleep)
    app = create_app(make_agent(), run_scheduler=False)

    async def attempt(ip, pw):
        transport = httpx.ASGITransport(app=app, client=(ip, 1))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            r = await c.post("/login", data={"password": pw}, headers=ORIGIN)
            return r.status_code

    async def scenario():
        bad = [asyncio.create_task(attempt(f"10.0.0.{i}", "wrong")) for i in range(6)]
        await real_sleep(0.5)  # all 6 wrong guesses checked and now sleeping
        t = time.perf_counter()
        good = await attempt("1.2.3.4", PW)
        took = time.perf_counter() - t
        return good, took, await asyncio.gather(*bad)

    good, took, bad = asyncio.run(scenario())
    assert good == 303 and took < 1.0, took
    assert bad == [401] * 6


def test_one_attempt_per_address_at_a_time(make_agent, monkeypatch):
    import asyncio
    import httpx
    real_sleep = asyncio.sleep

    async def slow_sleep(seconds):
        await real_sleep(1)

    monkeypatch.setattr("agent.web.app.asyncio.sleep", slow_sleep)
    app = create_app(make_agent(), run_scheduler=False)

    async def attempt(ip):
        transport = httpx.ASGITransport(app=app, client=(ip, 1))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            return (await c.post("/login", data={"password": "x"}, headers=ORIGIN)).status_code

    async def scenario():
        first = asyncio.create_task(attempt("2001:db8::1"))
        await real_sleep(0.3)
        second = await attempt("2001:db8::2")  # same /64, while the first is still sleeping
        return await first, second

    assert asyncio.run(scenario()) == (401, 429)


def test_forwarded_for_only_from_trusted_proxy(make_agent, monkeypatch, caplog):
    monkeypatch.setattr("agent.web.app.asyncio.sleep", _no_sleep)
    agent = make_agent(trusted_proxies=("172.17.0.1",))
    app = create_app(agent, run_scheduler=False)
    with TestClient(app, client=("172.17.0.1", 1)) as proxy:
        for i in range(auth.PER_IP_FAILS):  # 5 wrong from one visitor behind the proxy
            proxy.post("/login", data={"password": "x"},
                       headers={**ORIGIN, "X-Forwarded-For": "6.6.6.6"})
        r = proxy.post("/login", data={"password": PW},
                       headers={**ORIGIN, "X-Forwarded-For": "1.2.3.4"}, follow_redirects=False)
        assert r.status_code == 303  # a different visitor is not locked out
    with TestClient(app, client=("8.8.8.8", 1)) as direct:
        with caplog.at_level("WARNING", logger="agent.web"):
            direct.post("/login", data={"password": "x"},
                        headers={**ORIGIN, "X-Forwarded-For": "9.9.9.9"})
        assert "FORWARDED_ALLOW_IPS" in caplog.text


def test_forwarded_for_details():
    from agent.web.app import ProxyHeaders, _strip_port
    assert _strip_port("1.2.3.4:5678") == "1.2.3.4"
    assert _strip_port("[2001:db8::1]:443") == "2001:db8::1"
    assert _strip_port(" 2001:db8::1 ") == "2001:db8::1"
    seen = {}

    async def app(scope, receive, send):
        seen["client"] = scope["client"][0]

    async def run(trusted, peer, *lines):
        mw = ProxyHeaders(app, trusted)
        headers = [(b"x-forwarded-for", line.encode()) for line in lines]
        await mw({"type": "http", "client": (peer, 1), "headers": headers}, None, None)
        return seen["client"]

    import asyncio
    # A visitor typing a fake left-hand entry doesn't change who they are.
    with pytest.raises(ValueError):
        ProxyHeaders(app, ("*",))
    assert asyncio.run(run(("172.17.0.1",), "172.17.0.1", "1.1.1.1, 6.6.6.6:4000")) == "6.6.6.6"
    assert asyncio.run(run(("172.17.0.0/16",), "172.17.0.1", "1.1.1.1", "6.6.6.6")) == "6.6.6.6"
    assert asyncio.run(run(("172.17.0.0/16",), "172.17.0.1", "6.6.6.6, 172.17.0.9")) == "6.6.6.6"
    assert asyncio.run(run(("172.17.0.1",), "8.8.8.8", "1.1.1.1")) == "8.8.8.8"  # untrusted peer


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


@pytest.mark.parametrize("text", [" ", "\n\t", "\u2003"])
def test_blank_message_rejected_without_history_or_usage(client, text):
    login(client)
    chat = client.post("/api/chats", headers=ORIGIN).json()["id"]
    response = client.post(f"/api/chats/{chat}/send", json={"text": text}, headers=ORIGIN)
    assert response.status_code == 422
    assert client.agent.memory.messages(chat) == []
    assert client.agent.calls_left() == client.agent.config.daily_call_limit


@pytest.mark.parametrize("field", ["name", "prompt"])
def test_blank_job_rejected(client, field):
    login(client)
    body = {"name": "Morning", "cron": "0 7 * * *", "prompt": "Summarise"}
    body[field] = " \n\t"
    assert client.post("/api/jobs", json=body, headers=ORIGIN).status_code == 422
    assert client.agent.memory.jobs() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("scheduled", [False, True])
async def test_shutdown_cancels_and_awaits_job(make_agent, monkeypatch, scheduled):
    import asyncio
    import httpx
    from agent.scheduler import running_jobs

    agent = make_agent()
    started, stopped = asyncio.Event(), asyncio.Event()

    async def run_job(job):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(agent, "run_job", run_job)
    job_id = agent.memory.add_job("Long job", "* * * * *", "work", 0 if scheduled else 10**12)
    app = create_app(agent, run_scheduler=scheduled)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://testserver") as c:
            assert (await c.post("/login", data={"password": PW}, headers=ORIGIN)).status_code == 303
            status = (await c.get("/api/status")).json()
            headers = {**ORIGIN, "X-CSRF-Token": status["csrf"]}
            if not scheduled:
                assert (await c.post(f"/api/jobs/{job_id}/run", headers=headers)).status_code == 200
            await asyncio.wait_for(started.wait(), 2)
    assert stopped.is_set()
    assert job_id not in running_jobs
    assert agent.memory.runs()[0]["finished"] is not None


@pytest.mark.asyncio
async def test_forwarded_for_never_uses_leftmost_when_all_trusted(caplog):
    from agent.web.app import ProxyHeaders

    seen = []

    async def app(scope, receive, send):
        seen.append(scope["client"][0])

    middleware = ProxyHeaders(app, ("10.77.1.0/24",))
    scope = {"type": "http", "client": ("10.77.1.2", 1),
             "headers": [(b"x-forwarded-for", b"10.77.1.5, 10.77.1.6")]}
    with caplog.at_level("WARNING", logger="agent.web"):
        await middleware(scope, None, None)
        await middleware(scope, None, None)
    assert seen == ["10.77.1.2", "10.77.1.2"]
    assert caplog.text.count("keeping direct peer") == 1


def test_star_rejected_in_config_check(make_agent):
    agent = make_agent(trusted_proxies=("*",))
    with pytest.raises(SystemExit) as error:
        agent.config.check()
    assert str(error.value) == "FORWARDED_ALLOW_IPS='*' is not allowed; list the proxy IP"


@pytest.mark.asyncio
async def test_invalid_forwarded_hop_is_used_only_when_rightmost():
    from agent.web.app import ProxyHeaders

    seen = []

    async def app(scope, receive, send):
        seen.append(scope["client"][0])

    middleware = ProxyHeaders(app, ("10.77.1.2",))
    for hops in (b"invalid, 1.2.3.4", b"1.2.3.4, invalid"):
        await middleware({"type": "http", "client": ("10.77.1.2", 1),
                          "headers": [(b"x-forwarded-for", hops)]}, None, None)
    assert seen == ["1.2.3.4", "invalid"]
