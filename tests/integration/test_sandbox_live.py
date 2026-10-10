"""A4.3: live sandbox checks (marker integration). Needs compose test stack."""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.integration

SANDBOX_URL = os.environ.get("SANDBOX_URL", "http://10.77.3.20:7000")


def _token() -> str:
    path = os.environ.get("SANDBOX_API_TOKEN_FILE", "")
    if path and Path(path).is_file():
        return Path(path).read_text(encoding="utf-8").strip()
    token = os.environ.get("SANDBOX_API_TOKEN", "")
    if not token:
        pytest.skip("SANDBOX_API_TOKEN not available")
    return token


def _exec(command: str, timeout_s: int = 30) -> dict:
    headers = {"Authorization": f"Bearer {_token()}"}
    with httpx.Client(trust_env=False, timeout=timeout_s + 10) as client:
        response = client.post(
            f"{SANDBOX_URL.rstrip('/')}/v1/exec",
            json={"command": command, "timeout_s": timeout_s, "cwd": "."},
            headers=headers,
        )
    if response.status_code != 200:
        pytest.fail(f"sandbox exec failed HTTP {response.status_code}: {response.text[:300]}")
    return response.json()


def test_pwd_is_workspace():
    body = _exec("pwd")
    assert body["exit_code"] == 0
    assert body["output"].strip() == "/workspace"


def test_uid_is_1000():
    body = _exec("id -u")
    assert body["exit_code"] == 0
    assert body["output"].strip() == "1000"


def test_root_filesystem_is_read_only():
    body = _exec("touch /x")
    assert body["exit_code"] != 0
    assert "read-only" in body["output"].lower() or "read only" in body["output"].lower() or "Read-only" in body["output"]


def test_proc_environ_has_no_token():
    token = _token()
    body = _exec("cat /proc/1/environ | tr '\\0' '\\n' || true")
    assert token not in body["output"]
    # The name of the secret *file* is there (SANDBOX_API_TOKEN_FILE); the token itself is not.
    assert "SANDBOX_API_TOKEN=" not in body["output"]


def test_fork_bomb_does_not_kill_sandbox():
    # Guarded; pids_limit should contain it. sandboxd must stay healthy.
    _exec("timeout 5 bash -c ':(){ :|:& };:' || true", timeout_s=15)
    with httpx.Client(trust_env=False, timeout=5) as client:
        health = client.get(f"{SANDBOX_URL.rstrip('/')}/healthz")
    assert health.status_code == 200 and health.json() == {"ok": True}


def test_huge_alloc_dies_and_sandbox_recovers():
    body = _exec('python3 -c "x=bytearray(3*1024**3)"', timeout_s=30)
    # OOM kill, MemoryError, or non-zero exit — all acceptable.
    assert body["exit_code"] != 0 or "MemoryError" in body["output"] or body["timed_out"]
    with httpx.Client(trust_env=False, timeout=5) as client:
        health = client.get(f"{SANDBOX_URL.rstrip('/')}/healthz")
    assert health.status_code == 200 and health.json() == {"ok": True}


def test_sandbox_write_visible_in_workspace(tmp_path):
    # Workspace is shared; write a file via sandbox and read it from the mount if present.
    name = "a43-live.txt"
    body = _exec(f"echo live-ok > {name}")
    assert body["exit_code"] == 0
    workspace = Path(os.environ.get("WORKSPACE_DIR", "/workspace"))
    target = workspace / name
    if target.exists():
        assert "live-ok" in target.read_text()
    else:
        # When not sharing the same mount (unit host), at least confirm sandbox saw the write.
        listed = _exec(f"cat {name}")
        assert "live-ok" in listed["output"]
