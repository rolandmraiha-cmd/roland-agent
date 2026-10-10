"""sandboxd HTTP API: peer + Bearer gated shell execution (§6.3, §8.3)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import resource
import signal
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from .paths import WorkspaceError, resolve_cwd

log = logging.getLogger("sandboxd")

WORKSPACE = Path(os.environ.get("SANDBOX_WORKSPACE", "/workspace"))
MAX_BODY = 64 * 1024
MAX_COMMAND = 16000


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


def _read_token() -> str:
    path = os.environ.get("SANDBOX_API_TOKEN_FILE", "").strip()
    if path:
        return Path(path).read_text(encoding="utf-8").strip()
    return os.environ.get("SANDBOX_API_TOKEN", "").strip()


def _peers() -> set[str]:
    raw = os.environ.get("SANDBOXD_ALLOWED_PEERS", "10.77.3.10")
    peers = {p.strip() for p in raw.split(",") if p.strip()}
    peers.update({"127.0.0.1", "::1"})
    return peers


def _prctl_dumpable() -> None:
    if not sys.platform.startswith("linux"):
        return
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE
            log.warning("prctl(PR_SET_DUMPABLE) failed")
    except (OSError, AttributeError):
        log.warning("could not call prctl")


def _minimal_env(tz: str) -> dict[str, str]:
    home = str(WORKSPACE / ".sandbox-home")
    return {
        "PATH": f"{home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": home,
        "LANG": "C.UTF-8",
        "TZ": tz,
        "TERM": "dumb",
        "PYTHONUNBUFFERED": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }


def _preexec(timeout_s: int) -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (timeout_s + 5, timeout_s + 5))
    resource.setrlimit(resource.RLIMIT_FSIZE, (2 * 1024**3, 2 * 1024**3))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (1024, 1024))


def _killpg(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _in_container() -> bool:
    if Path("/.dockerenv").exists():
        return True
    flag = os.environ.get("SANDBOX_REAP_ALL", "").strip().lower()
    return flag in {"1", "true", "yes"}


def _reap_leftovers(self_pid: int) -> None:
    """SIGKILL every process in the container except PID 1 (tini) and sandboxd.

    Only runs inside Docker (or when SANDBOX_REAP_ALL is set). A host-wide
    /proc scan would kill the developer machine — never do that in unit tests.
    """
    if not _in_container():
        return
    if not Path("/proc").is_dir():
        return
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid in {1, self_pid}:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


class SandboxState:
    def __init__(self) -> None:
        self.token = _read_token()
        if not self.token:
            raise SystemExit("SANDBOX_API_TOKEN or SANDBOX_API_TOKEN_FILE is required")
        self.peers = _peers()
        self.max_output = _env_int("SANDBOX_MAX_OUTPUT_BYTES", 65536)
        self.max_concurrent = _env_int("SANDBOX_MAX_CONCURRENT", 2)
        self.timeout_max = _env_int("SANDBOX_TIMEOUT_MAX", 300)
        self.tz = os.environ.get("TZ", "Europe/Helsinki")
        self.host = os.environ.get("SANDBOXD_HOST", "10.77.3.20")
        self.port = _env_int("SANDBOXD_PORT", 7000)
        self.slots = asyncio.Semaphore(self.max_concurrent)
        self.active = 0
        self.lock = asyncio.Lock()
        WORKSPACE.mkdir(parents=True, exist_ok=True)
        (WORKSPACE / ".sandbox-home").mkdir(parents=True, exist_ok=True)
        _prctl_dumpable()

    async def acquire(self) -> bool:
        try:
            await asyncio.wait_for(self.slots.acquire(), timeout=30)
        except TimeoutError:
            return False
        async with self.lock:
            self.active += 1
        return True

    async def release(self) -> None:
        async with self.lock:
            self.active -= 1
            idle = self.active <= 0
        self.slots.release()
        if idle:
            await asyncio.to_thread(_reap_leftovers, os.getpid())


def create_app(state: SandboxState | None = None) -> FastAPI:
    state = state or SandboxState()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.sandbox = state

    @app.middleware("http")
    async def peer_and_body(request: Request, call_next):
        peer = request.client.host if request.client else ""
        # Own bind address is allowed for Docker healthchecks (core PeerAllowlist pattern).
        if peer not in state.peers and peer != state.host:
            return JSONResponse({"error": "peer not allowed"}, status_code=403)
        if request.url.path != "/healthz":
            auth = request.headers.get("authorization", "")
            expected = f"Bearer {state.token}"
            if not hmac.compare_digest(auth, expected):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            body = await request.body()
            if len(body) > MAX_BODY:
                return JSONResponse({"error": "body too large"}, status_code=413)

            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}

            request = Request(request.scope, receive)
        return await call_next(request)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.post("/v1/exec")
    async def exec_command(request: Request) -> Response:
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError, UnicodeError):
            return JSONResponse({"error": "invalid json"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "invalid body"}, status_code=400)
        command = payload.get("command", "")
        if not isinstance(command, str) or not command or len(command) > MAX_COMMAND:
            return JSONResponse({"error": "invalid command"}, status_code=400)
        try:
            timeout_s = int(payload.get("timeout_s", 60))
        except (TypeError, ValueError):
            return JSONResponse({"error": "invalid timeout_s"}, status_code=400)
        if timeout_s < 1 or timeout_s > state.timeout_max:
            return JSONResponse({"error": "timeout_s out of range"}, status_code=400)
        cwd_raw = payload.get("cwd", ".")
        if not isinstance(cwd_raw, str):
            return JSONResponse({"error": "invalid cwd"}, status_code=400)
        try:
            cwd = resolve_cwd(WORKSPACE, cwd_raw)
        except WorkspaceError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        if not await state.acquire():
            return JSONResponse({"error": "busy"}, status_code=429)

        exec_id = str(uuid.uuid4())
        started = time.monotonic()
        truncated = False
        timed_out = False
        exit_code = -1
        output = b""
        try:
            env = _minimal_env(state.tz)
            proc = await asyncio.create_subprocess_exec(
                "/bin/bash",
                "--noprofile",
                "--norc",
                "-c",
                command,
                cwd=str(cwd),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
                preexec_fn=lambda: _preexec(timeout_s),
            )

            async def read_capped() -> tuple[bytes, bool]:
                out = b""
                assert proc.stdout is not None
                while len(out) <= state.max_output:
                    part = await proc.stdout.read(4096)
                    if not part:
                        return out, False
                    out += part
                _killpg(proc.pid)
                return out[: state.max_output], True

            async def finish() -> None:
                try:
                    await asyncio.wait_for(proc.communicate(), timeout=5)
                except TimeoutError:
                    _killpg(proc.pid)

            try:
                output, truncated = await asyncio.wait_for(read_capped(), timeout=timeout_s)
                await finish()
            except TimeoutError:
                timed_out = True
                _killpg(proc.pid)
                await finish()
            exit_code = proc.returncode if proc.returncode is not None else -1
            if timed_out:
                exit_code = -1
        finally:
            await state.release()

        duration_ms = int((time.monotonic() - started) * 1000)
        text = output.decode("utf-8", errors="replace")
        preview = command[:200]
        log_line = {
            "ts": time.time(),
            "exec_id": exec_id,
            "command_sha256": hashlib.sha256(command.encode()).hexdigest(),
            "command_preview": preview,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "truncated": truncated,
            "timed_out": timed_out,
        }
        print(json.dumps(log_line, separators=(",", ":")), flush=True)
        return JSONResponse(
            {
                "exec_id": exec_id,
                "exit_code": exit_code,
                "output": text,
                "truncated": truncated,
                "timed_out": timed_out,
                "duration_ms": duration_ms,
            }
        )

    return app


def serve() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    state = SandboxState()
    app = create_app(state)
    uvicorn.run(
        app,
        host=state.host,
        port=state.port,
        proxy_headers=False,
        server_header=False,
        log_level="info",
    )


def healthcheck_cli() -> bool:
    host = os.environ.get("SANDBOXD_HOST", "10.77.3.20")
    port = _env_int("SANDBOXD_PORT", 7000)
    try:
        with httpx.Client(trust_env=False, timeout=3) as client:
            response = client.get(f"http://{host}:{port}/healthz")
        payload = response.json()
        return response.status_code == 200 and payload == {"ok": True}
    except (httpx.HTTPError, ValueError):
        return False
