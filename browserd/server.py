"""browserd HTTP API (§8.4): exactly these routes, for core only.

Every /v1 route needs core's address as the peer and the Bearer token. There is no route
for cookies, storage, field values, running script, tracing or the debugging protocol, and
none may be added (docs/NEXT.md, M6 security requirements).
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import sys
import time
import urllib.request
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from . import guards
from .session import BrowserdError, Session
from .settings import MAX_BODY, MAX_SNAPSHOT_CHARS, MAX_TEXT_CHARS, MAX_URL_CHARS, Settings

log = logging.getLogger("browserd")

# Still answered while Roland has the controls (user mode); everything else gets 423.
OPEN_IN_USER_MODE = frozenset({"/healthz", "/v1/status", "/v1/user-mode", "/v1/vnc/disconnect"})
MODES = frozenset({"safe", "approved"})


def _prctl_dumpable() -> None:
    """Stop other processes of this user from reading our memory (the token lives there)."""
    if not sys.platform.startswith("linux"):
        return
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE
            log.warning("prctl(PR_SET_DUMPABLE) failed")
    except (OSError, AttributeError):
        log.warning("could not call prctl")


def _bad() -> BrowserdError:
    return BrowserdError("bad_request", 400)


def _mode(body: dict) -> str:
    mode = body.get("mode", "safe")
    if mode not in MODES:
        raise _bad()
    return mode


def _flag(body: dict, name: str, default: bool = False) -> bool:
    value = body.get(name, default)
    if not isinstance(value, bool):
        raise _bad()
    return value


def create_app(settings: Settings, session, *, manage_session: bool = False) -> FastAPI:
    """`session` is a browserd.session.Session, or a stand-in with the same methods in tests."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if manage_session:
            await session.start()
        try:
            yield
        finally:
            if manage_session:
                await session.stop()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    expected = f"Bearer {settings.token}".encode()
    local = {"127.0.0.1", "::1", settings.host}  # the container's own health check

    async def read_body(request: Request) -> bytes | None:
        """The request body, or None when it is larger than allowed. Never reads past the cap."""
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_BODY:
            return None
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_BODY:
                return None
        return body

    @app.middleware("http")
    async def gate(request: Request, call_next):
        started = time.monotonic()
        path = request.url.path
        peer = request.client.host if request.client else ""
        if peer not in settings.peers and peer not in local:
            return JSONResponse({"error": "peer not allowed"}, status_code=403)
        if path != "/healthz":
            given = request.headers.get("authorization", "").encode("utf-8", "replace")
            if not hmac.compare_digest(given, expected):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            body = await read_body(request)
            if body is None:
                return JSONResponse({"error": "body too large"}, status_code=413)
            if session.mode == "user" and path not in OPEN_IN_USER_MODE:
                return JSONResponse({"error": "user_mode"}, status_code=423)
            request.state.body = body
        try:
            response = await call_next(request)
        except BrowserdError as error:
            response = JSONResponse({"error": error.code}, status_code=error.status)
        except Exception as error:  # a bug here must not take the service down or leak detail
            log.warning("unexpected %s on %s", type(error).__name__, path)
            response = JSONResponse({"error": "failed"}, status_code=500)
        if path != "/healthz":
            # One line per call: which route, how it ended, how long. Never addresses, page
            # text or what was typed.
            line = {"ts": round(time.time(), 3), "route": path[:60], "status": response.status_code,
                    "ms": int((time.monotonic() - started) * 1000)}
            print(json.dumps(line, separators=(",", ":")), flush=True)
        return response

    @app.exception_handler(BrowserdError)
    async def refused(_request: Request, error: BrowserdError) -> JSONResponse:
        return JSONResponse({"error": error.code}, status_code=error.status)

    def body_of(request: Request) -> dict:
        raw = getattr(request.state, "body", b"")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError) as error:
            raise _bad() from error
        if not isinstance(data, dict):
            raise _bad()
        return data

    def answer(data) -> JSONResponse:
        return JSONResponse(guards.clean(data))

    def ref_and_print(body: dict) -> tuple[str, str]:
        ref, fingerprint = body.get("ref"), body.get("fingerprint")
        if not guards.valid_ref(ref):
            raise BrowserdError("no_such_element", 404)
        if not isinstance(fingerprint, str) or not 16 <= len(fingerprint) <= 128:
            raise _bad()
        return ref, fingerprint

    @app.get("/healthz")
    async def healthz() -> dict:
        return session.health()

    @app.get("/v1/status")
    async def status():
        return answer(await session.status())

    @app.post("/v1/navigate")
    async def navigate(request: Request):
        body = body_of(request)
        url = body.get("url")
        if not isinstance(url, str) or not url.strip() or len(url) > MAX_URL_CHARS:
            raise _bad()
        return answer(await session.navigate(url.strip(), _flag(body, "new_tab")))

    @app.post("/v1/snapshot")
    async def snapshot(request: Request):
        size = body_of(request).get("max_chars", 6000)
        if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= MAX_SNAPSHOT_CHARS:
            raise _bad()
        return answer(await session.snapshot(size))

    @app.post("/v1/describe")
    async def describe(request: Request):
        body = body_of(request)
        if body.get("focused") is True:
            return answer(await session.describe(focused=True))
        if not guards.valid_ref(body.get("ref")):
            raise BrowserdError("no_such_element", 404)
        return answer(await session.describe(ref=body["ref"]))

    @app.post("/v1/click")
    async def click(request: Request):
        body = body_of(request)
        ref, fingerprint = ref_and_print(body)
        return answer(await session.click(ref, fingerprint, _mode(body)))

    @app.post("/v1/type")
    async def type_text(request: Request):
        body = body_of(request)
        ref, fingerprint = ref_and_print(body)
        text = body.get("text")
        if not isinstance(text, str) or len(text) > MAX_TEXT_CHARS:
            raise _bad()
        return answer(await session.type(
            ref, fingerprint, text, clear=_flag(body, "clear", True), submit=_flag(body, "submit"),
            mode=_mode(body),
        ))

    @app.post("/v1/press")
    async def press(request: Request):
        body = body_of(request)
        key = body.get("key")
        if not isinstance(key, str) or key not in guards.KEYS:
            raise BrowserdError("bad_key", 400)
        fingerprint = body.get("fingerprint")
        if fingerprint is not None and (not isinstance(fingerprint, str) or len(fingerprint) > 128):
            raise _bad()
        return answer(await session.press(key, _mode(body), fingerprint or None))

    @app.post("/v1/select")
    async def select(request: Request):
        body = body_of(request)
        ref, fingerprint = ref_and_print(body)
        values = body.get("values")
        if (
            not isinstance(values, list)
            or not 1 <= len(values) <= 20
            or any(not isinstance(value, str) or len(value) > 200 for value in values)
        ):
            raise _bad()
        return answer(await session.select(ref, fingerprint, values, _mode(body)))

    @app.post("/v1/scroll")
    async def scroll(request: Request):
        body = body_of(request)
        direction, pages = body.get("direction"), body.get("pages", 1)
        if direction not in {"up", "down"} or isinstance(pages, bool) or not isinstance(pages, int) or not 1 <= pages <= 10:
            raise _bad()
        return answer(await session.scroll(direction, pages))

    @app.post("/v1/back")
    async def back():
        return answer(await session.history(forward=False))

    @app.post("/v1/forward")
    async def forward():
        return answer(await session.history(forward=True))

    @app.post("/v1/tabs/{tab_id}/activate")
    async def activate_tab(tab_id: str):
        if not guards.valid_tab(tab_id):
            raise BrowserdError("no_such_tab", 404)
        return answer(await session.activate_tab(tab_id))

    @app.post("/v1/tabs/{tab_id}/close")
    async def close_tab(tab_id: str):
        if not guards.valid_tab(tab_id):
            raise BrowserdError("no_such_tab", 404)
        return answer(await session.close_tab(tab_id))

    @app.post("/v1/screenshot")
    async def screenshot(request: Request):
        data = await session.screenshot(_flag(body_of(request), "full_page"))
        return Response(data, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.post("/v1/upload")
    async def upload(request: Request):
        body = body_of(request)
        ref, fingerprint = ref_and_print(body)
        return answer(await session.upload(ref, fingerprint, body.get("path"), body.get("sha256")))

    @app.get("/v1/downloads")
    async def downloads():
        return answer(await session.downloads())

    @app.post("/v1/user-mode")
    async def user_mode(request: Request):
        body = body_of(request)
        if not isinstance(body.get("on"), bool):
            raise _bad()
        return answer(await session.set_user_mode(body["on"]))

    @app.post("/v1/vnc/disconnect")
    async def vnc_disconnect():
        return {"ok": True}  # nothing to disconnect until the screen exists (M7)

    return app


def serve() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = Settings.from_env()
    if settings.allow_private_hosts:
        log.warning("BROWSER_ALLOW_PRIVATE_HOSTS is set: private addresses are reachable. Tests only.")
    _prctl_dumpable()
    session = Session(settings)
    app = create_app(settings, session, manage_session=True)
    server = uvicorn.Server(uvicorn.Config(
        app, host=settings.host, port=settings.port, proxy_headers=False, server_header=False,
        access_log=False, log_level="warning", timeout_graceful_shutdown=20,
    ))
    died = []

    def on_dead() -> None:  # Chromium went away: leave, and the launcher starts us again
        died.append(True)
        server.should_exit = True

    session.on_dead = on_dead
    server.run()
    if died:
        raise SystemExit(1)


def healthcheck_cli() -> bool:
    """The container's health check. Standard library only, and never through a proxy."""
    host = os.environ.get("BROWSERD_HOST", "").strip() or "10.77.4.40"
    port = os.environ.get("BROWSERD_PORT", "").strip() or "7100"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://{host}:{port}/healthz", timeout=3) as response:  # noqa: S310 -- fixed http address
            payload = json.loads(response.read(4096))
        return isinstance(payload, dict) and payload.get("ok") is True and payload.get("browser") is True
    except (OSError, ValueError):
        return False
