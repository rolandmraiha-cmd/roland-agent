"""A4.1: in-process sandboxd unit tests with a temp workspace."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport

# Async tests are marked individually (sync gate test must not carry asyncio mark).


def _make_app(tmp_path: Path, *, token: str = "test-token-abc", peers: str = "10.77.3.10"):
    os.environ["SANDBOX_WORKSPACE"] = str(tmp_path)
    os.environ["SANDBOX_API_TOKEN"] = token
    os.environ.pop("SANDBOX_API_TOKEN_FILE", None)
    os.environ["SANDBOXD_ALLOWED_PEERS"] = peers
    os.environ["SANDBOXD_HOST"] = "10.77.3.20"
    os.environ["SANDBOX_MAX_OUTPUT_BYTES"] = "4096"
    os.environ["SANDBOX_MAX_CONCURRENT"] = "2"
    os.environ["SANDBOX_TIMEOUT_MAX"] = "30"
    os.environ["TZ"] = "Europe/Helsinki"
    # Fresh module state for each test.
    import importlib

    import sandboxd.server as server

    importlib.reload(server)
    state = server.SandboxState()
    return server.create_app(state), token


def _client(app, peer: str = "10.77.3.10") -> httpx.AsyncClient:
    transport = ASGITransport(app=app, client=(peer, 12345))
    return httpx.AsyncClient(transport=transport, base_url="http://sandbox.test", trust_env=False)


@pytest.mark.asyncio
async def test_requires_token(tmp_path):
    app, token = _make_app(tmp_path)
    async with _client(app) as client:
        ok = await client.get("/healthz")
        assert ok.status_code == 200 and ok.json() == {"ok": True}
        denied = await client.post("/v1/exec", json={"command": "echo hi", "timeout_s": 5})
        assert denied.status_code == 401
        allowed = await client.post(
            "/v1/exec",
            json={"command": "echo hi", "timeout_s": 5},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert allowed.status_code == 200
        assert "hi" in allowed.json()["output"]


@pytest.mark.asyncio
async def test_rejects_wrong_peer(tmp_path):
    app, token = _make_app(tmp_path)
    async with _client(app, peer="10.77.3.99") as client:
        response = await client.get("/healthz")
        assert response.status_code == 403
        response = await client.post(
            "/v1/exec",
            json={"command": "echo hi", "timeout_s": 5},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403


@pytest.mark.asyncio
async def test_timeout_kills_process_group(tmp_path):
    app, token = _make_app(tmp_path)
    async with _client(app) as client:
        started = time.monotonic()
        response = await client.post(
            "/v1/exec",
            json={"command": "sleep 30", "timeout_s": 1},
            headers={"Authorization": f"Bearer {token}"},
        )
        elapsed = time.monotonic() - started
        assert response.status_code == 200
        body = response.json()
        assert body["timed_out"] is True
        assert elapsed < 10


@pytest.mark.asyncio
async def test_output_cap_truncates_and_kills(tmp_path):
    app, token = _make_app(tmp_path)
    async with _client(app) as client:
        response = await client.post(
            "/v1/exec",
            json={"command": "yes x", "timeout_s": 10},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["truncated"] is True
        assert len(body["output"].encode()) <= 4096 + 64


@pytest.mark.asyncio
async def test_env_is_minimal_no_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOX_API_TOKEN", "should-not-leak")
    monkeypatch.setenv("MODEL_SERVER_TOKEN", "model-secret")
    monkeypatch.setenv("AGENT_PASSWORD_HASH", "hash-secret")
    app, token = _make_app(tmp_path, token="real-token-value")
    async with _client(app) as client:
        response = await client.post(
            "/v1/exec",
            json={"command": "env", "timeout_s": 5},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        out = response.json()["output"]
        for name in (
            "SANDBOX_API_TOKEN",
            "MODEL_SERVER_TOKEN",
            "AGENT_PASSWORD_HASH",
            "real-token-value",
            "model-secret",
            "hash-secret",
            "should-not-leak",
        ):
            assert name not in out
        assert "HOME=" in out and ".sandbox-home" in out


@pytest.mark.asyncio
async def test_cwd_must_stay_in_workspace(tmp_path):
    app, token = _make_app(tmp_path)
    async with _client(app) as client:
        response = await client.post(
            "/v1/exec",
            json={"command": "pwd", "timeout_s": 5, "cwd": "../"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 400
        assert "outside" in response.json()["error"]
        response = await client.post(
            "/v1/exec",
            json={"command": "pwd", "timeout_s": 5, "cwd": "/etc"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 400


def test_reap_gate_refuses_host_wide_scan(monkeypatch):
    """Unit safety: when _in_container is false, _reap_leftovers must not os.kill anyone."""
    import importlib

    import sandboxd.server as server

    importlib.reload(server)
    monkeypatch.delenv("SANDBOX_REAP_ALL", raising=False)
    # Real host without Docker: the gate itself must stay closed.
    if not Path("/.dockerenv").exists():
        assert server._in_container() is False
    monkeypatch.setattr(server, "_in_container", lambda: False)
    calls: list[tuple] = []
    monkeypatch.setattr(server.os, "kill", lambda pid, sig: calls.append((pid, sig)))
    server._reap_leftovers(os.getpid())
    assert calls == [], f"reap must be a no-op outside container, got {calls}"


@pytest.mark.asyncio
async def test_leftover_background_process_is_reaped(tmp_path, monkeypatch):
    """Release path invokes reap; on the host mock reap to kill only the marker PID.

    Real /proc-wide SIGKILL is covered by docker-compose.test.yml integration, never here.
    """
    import signal

    app, token = _make_app(tmp_path)
    import sandboxd.server as server  # after reload inside _make_app

    marker = tmp_path / "reap-marker"
    reaped: list[int] = []

    def mock_reap(self_pid: int) -> None:
        # Never scan /proc on the developer host. Only the sleep we spawned.
        if not marker.exists():
            return
        text = marker.read_text().strip()
        if not text.isdigit():
            return
        pid = int(text)
        try:
            os.kill(pid, signal.SIGKILL)
            reaped.append(pid)
        except ProcessLookupError:
            pass

    monkeypatch.setattr(server, "_reap_leftovers", mock_reap)
    async with _client(app) as client:
        response = await client.post(
            "/v1/exec",
            json={
                "command": f"setsid sleep 1000 & echo $! > {marker.name}; sleep 0.2",
                "timeout_s": 5,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
    await asyncio.sleep(0.3)
    assert reaped, "release() must call _reap_leftovers when idle"
    if marker.exists():
        pid_text = marker.read_text().strip()
        if pid_text.isdigit():
            assert not Path(f"/proc/{int(pid_text)}").exists()


@pytest.mark.asyncio
async def test_concurrency_limit_429(tmp_path):
    app, token = _make_app(tmp_path)
    # Force a single slot so the third waiter trips the 30s wait more quickly —
    # use two long sleepers to fill both slots, then a third should 429.
    os.environ["SANDBOX_MAX_CONCURRENT"] = "1"
    import importlib

    import sandboxd.server as server

    importlib.reload(server)
    os.environ["SANDBOX_WORKSPACE"] = str(tmp_path)
    os.environ["SANDBOX_API_TOKEN"] = token
    state = server.SandboxState()
    # Shrink wait by patching acquire timeout via monkeypatch of wait_for — override acquire.
    async def fast_acquire():
        try:
            await asyncio.wait_for(state.slots.acquire(), timeout=0.2)
        except TimeoutError:
            return False
        async with state.lock:
            state.active += 1
        return True

    state.acquire = fast_acquire  # type: ignore[method-assign]
    app = server.create_app(state)

    async with _client(app) as client:
        headers = {"Authorization": f"Bearer {token}"}

        async def long_job():
            return await client.post(
                "/v1/exec",
                json={"command": "sleep 2", "timeout_s": 5},
                headers=headers,
            )

        first = asyncio.create_task(long_job())
        await asyncio.sleep(0.05)
        second = await client.post(
            "/v1/exec",
            json={"command": "echo late", "timeout_s": 5},
            headers=headers,
        )
        assert second.status_code == 429
        assert (await first).status_code == 200
