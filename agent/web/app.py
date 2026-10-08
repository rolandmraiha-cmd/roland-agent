"""The web chat page and its API, served by the agent itself."""

from __future__ import annotations

import asyncio
import html
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from ..config import validate_agent_host
from ..core import Agent, sse
from ..models.modelreg import load_model_info
from ..schedule import next_run_after, valid_cron
from ..scheduler import MAX_PARALLEL_JOBS, execute, running_jobs, scheduler_loop
from .auth import (
    MAX_WAITING,
    LoginGate,
    LoginLimiter,
    Sessions,
    client_key,
    cookie_name,
    csrf_token,
    password_ok,
    token_hash,
)
from .middleware import (
    AuthMiddleware,
    CSRFMiddleware,
    PeerAllowlist,
    SecurityHeaders,
    security_headers,
)
from .middleware import (
    ProxyHeaders as ProxyHeaders,
)
from .middleware import (
    _strip_port as _strip_port,
)
from .routes_approvals import build_router as build_approvals_router
from .routes_browser import browser_status
from .routes_browser import build_router as build_browser_router
from .routes_files import build_router as build_files_router
from .routes_screen import build_router as build_screen_router

STATIC = Path(__file__).parent / "static"
SCREEN_TICK_S = 10  # how often idle screen sessions are ended and browserd's mode is checked


class SendBody(BaseModel):
    text: str = Field(min_length=1, max_length=20000)

    @field_validator("text")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value


class JobBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    cron: str = Field(min_length=9, max_length=100)
    prompt: str = Field(min_length=1, max_length=5000)

    @field_validator("name", "prompt")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name and prompt must not be blank")
        return value


log = logging.getLogger("agent.web")


def create_app(agent: Agent, run_scheduler: bool = True) -> FastAPI:
    config = agent.config
    validate_agent_host(config.agent_host)
    cookie = cookie_name(config)
    sessions = Sessions(agent.memory, config.session_days, config.idle_hours, config.password_hash)
    limiter = LoginLimiter()
    gate = LoginGate()
    busy: set[int] = set()
    tasks: set[asyncio.Task] = set()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if n := agent.gate.expire_on_startup():
            log.info("expired %s pending approval(s) on startup", n)
        if run_scheduler and (n := agent.memory.fail_unfinished_runs()):
            log.info("marked %s unfinished job run(s) as failed", n)
        agent.audit.write("system", "startup", detail={"scheduler": run_scheduler})
        loop_task = asyncio.create_task(scheduler_loop(agent)) if run_scheduler else None
        screen_task = None
        if agent.screens.available:
            # Nothing from before the restart is still waiting or watching. Records first;
            # the loop then tells browserd, so starting never waits for it.
            if n := agent.signins.reset_on_startup():
                log.info("cancelled %s waiting sign-in(s) on startup", n)
            agent.screens.reset_on_startup()
            screen_task = asyncio.create_task(screen_loop())
        try:
            yield
        finally:
            # Await cancellation so jobs can finish their failure records and release slots.
            pending = list(tasks)
            if loop_task:
                pending.append(loop_task)
            if screen_task:
                pending.append(screen_task)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    async def screen_loop() -> None:
        while True:
            try:
                await agent.screens.tick()
            except Exception:  # housekeeping must survive one bad round
                log.exception("screen housekeeping failed")
            await asyncio.sleep(SCREEN_TICK_S)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def logged_in(request: Request) -> bool:
        return sessions.valid(request.cookies.get(cookie))

    # Access checks run Peer -> Proxy -> Host -> Auth -> CSRF. The outer header wrapper
    # also secures their refusal responses; Starlette's error handler covers uncaught errors.
    app.add_middleware(CSRFMiddleware, config=config)
    app.add_middleware(AuthMiddleware, config=config, sessions=sessions)
    if config.allowed_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=config.allowed_hosts, www_redirect=False)
    app.add_middleware(ProxyHeaders, trusted=config.trusted_proxies)
    app.add_middleware(PeerAllowlist, config=config)
    app.add_middleware(SecurityHeaders, config=config)

    @app.exception_handler(Exception)
    async def server_error(request: Request, error: Exception):
        return JSONResponse(
            {"error": "internal server error"},
            status_code=500,
            headers={**security_headers(config), "Cache-Control": "no-store"},
        )

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    # Flatten included routes so app.routes entries expose .path (v1 auth scan test).
    for _route in build_approvals_router(agent).routes:
        app.routes.append(_route)
    for _route in build_files_router(agent).routes:
        app.routes.append(_route)
    for _route in build_browser_router(agent).routes:
        app.routes.append(_route)
    # Before the /internal stub below, so the real screen-auth route answers first.
    for _route in build_screen_router(agent, sessions, cookie, STATIC).routes:
        app.routes.append(_route)


    @app.api_route("/internal", methods=["GET", "POST", "HEAD", "OPTIONS"])
    @app.api_route("/internal/{rest:path}", methods=["GET", "POST", "HEAD", "OPTIONS"])
    async def internal_stub(request: Request):
        # AuthMiddleware has already checked the original proxy peer. GET /internal/screen-auth
        # is answered by routes_screen; everything else under /internal stays closed.
        return JSONResponse(
            {"error": "unavailable"}, status_code=403 if request.url.path == "/internal/screen-auth" else 404
        )

    # --- login ---
    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    def login_page(message: str = "", status: int = 200) -> HTMLResponse:
        page = (STATIC / "login.html").read_text()
        page = page.replace("{{name}}", html.escape(config.agent_name))
        page = page.replace("{{message}}", html.escape(message))
        return HTMLResponse(page, status_code=status)

    @app.get("/login")
    async def login_form(request: Request):
        if logged_in(request):
            return RedirectResponse("/", status_code=303)
        return login_page()

    @app.post("/login")
    async def login(request: Request):
        # Behind a reverse proxy this is the real visitor's address only when the proxy's IP is
        # in FORWARDED_ALLOW_IPS (see .env.example).
        ip = client_key(request.client.host if request.client else "?")
        form = await request.form()
        given = str(form.get("password", ""))[:1024]

        def failed(reason: str) -> None:
            agent.audit.write("roland", "login_fail", detail={"client": ip, "reason": reason})

        def locked_page(wait: float) -> HTMLResponse:
            return login_page(f"Too many wrong passwords. Try again in {int(wait // 60) + 1} min.", 429)

        # A locked address is turned away before it can take a place in the queue.
        if wait := limiter.locked(ip):
            failed("locked")
            return locked_page(wait)
        if ip in gate.inflight:
            failed("inflight")
            return login_page("A login from your address is already being checked.", 429)
        if gate.waiting >= MAX_WAITING:
            failed("busy")
            return login_page("The login is busy. Try again in a moment.", 429)
        gate.inflight.add(ip)
        try:
            gate.waiting += 1
            try:
                # Check the lockout, verify and record the result as one step, one login at a
                # time. Only that happens under the lock; the slowdown runs after releasing it.
                async with gate.lock:
                    if wait := limiter.locked(ip):
                        failed("locked")
                        return locked_page(wait)
                    ok = await asyncio.to_thread(password_ok, given, config.password_hash)
                    if ok:
                        limiter.succeeded(ip)
                    else:
                        limiter.failed(ip)
                        failed("wrong_password")
                        delay = 1 + limiter.slowdown()
            finally:
                gate.waiting -= 1
            if not ok:
                # Slows this address's guessing without holding up anyone else. Its place in
                # `inflight` is kept meanwhile, so it can't send the next guess in parallel.
                await asyncio.sleep(delay)
                return login_page("Wrong password.", 401)
        finally:
            gate.inflight.discard(ip)
        response = RedirectResponse("/", status_code=303)
        with agent.memory.transaction():
            token = sessions.create()
            agent.audit.write("roland", "login_ok", detail={"client": ip})
        response.set_cookie(
            cookie,
            token,
            max_age=config.session_days * 86400,
            httponly=True,
            secure=config.cookie_secure,
            samesite="strict",
            path="/",
        )
        return response

    @app.post("/logout")
    async def logout(request: Request):
        token = request.cookies.get(cookie)
        if token:
            # His screen ends with his login: the page is cut off and the agent gets the
            # browser back, unless a sign-in is still waiting for him.
            await agent.screens.end_for_login(token_hash(token))
        with agent.memory.transaction():
            sessions.end(token)
            agent.audit.write("roland", "logout")
        response = JSONResponse({"ok": True})
        response.delete_cookie(
            cookie, path="/", secure=config.cookie_secure, httponly=True, samesite="strict"
        )
        return response

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    # --- chats ---
    @app.get("/api/status")
    async def status(request: Request):
        info = load_model_info(config.model_provider)
        browser = await browser_status(agent)
        return {
            "name": config.agent_name,
            "model": config.model_name,
            "model_info": None if info is None else info.public_dict(),
            "csrf": csrf_token(request.cookies.get(cookie, "")),
            "calls_left": agent.calls_left(),
            "daily_limit": config.daily_call_limit,
            "shell": agent.allow_shell,
            "pending_approvals": agent.memory.count_pending_approvals(),
            "last_backup_ok": agent.memory.get_meta("last_backup_ok"),
            "last_backup_error": agent.memory.get_meta("last_backup_error"),
            "pending_signins": len(agent.signins.active()),
            "browser": {"enabled": browser["enabled"], "mode": browser.get("mode"), "url": browser.get("url")},
            "screen": {"enabled": agent.screens.available},
        }

    @app.get("/api/chats")
    async def chats():
        return agent.memory.chats()

    @app.post("/api/chats")
    async def new_chat():
        return {"id": agent.memory.new_chat()}

    def need_chat(chat_id: int) -> None:
        if not agent.memory.chat_exists(chat_id):
            raise HTTPException(404, "no such chat")

    @app.get("/api/chats/{chat_id}/messages")
    async def messages(chat_id: int):
        need_chat(chat_id)
        timeline = agent.memory.timeline(chat_id)
        events = [row for row in timeline if row["kind"] != "text"]
        return {
            "messages": agent.memory.messages(chat_id),
            "events": events,
            "busy": chat_id in busy,
            "pending_approvals": agent.memory.approvals(status="pending", chat_id=chat_id),
            "pending_signins": agent.signins.active(chat_id),
        }

    @app.delete("/api/chats/{chat_id}")
    async def delete_chat(chat_id: int):
        if chat_id in busy:
            raise HTTPException(409, "the agent is still answering in this chat")
        need_chat(chat_id)
        agent.memory.delete_chat(chat_id)
        return {"ok": True}

    @app.post("/api/chats/{chat_id}/stop")
    async def stop_chat(chat_id: int):
        need_chat(chat_id)
        ok = await agent.stop_chat(chat_id)
        return {"ok": True, "stopped": ok}

    @app.post("/api/chats/{chat_id}/send")
    async def send(chat_id: int, body: SendBody):
        need_chat(chat_id)
        if chat_id in busy:
            raise HTTPException(409, "the agent is still answering in this chat")
        busy.add(chat_id)
        queue: asyncio.Queue = asyncio.Queue()

        # The answer runs as its own task, so it finishes and is saved even if the phone
        # closes the page halfway.
        async def produce():
            try:
                async for event in agent.chat(chat_id, body.text):
                    await queue.put(event)
            except asyncio.CancelledError:
                await queue.put({"type": "done", "reply": "[stopped by Roland]"})
            except Exception as e:
                await queue.put({"type": "error", "message": f"{type(e).__name__}: {e}"})
            finally:
                busy.discard(chat_id)
                agent._chat_tasks.pop(chat_id, None)
                await queue.put(None)

        task = asyncio.create_task(produce())
        tasks.add(task)
        agent._chat_tasks[chat_id] = task
        task.add_done_callback(tasks.discard)

        async def stream():
            # Keepalive comments reset mobile/proxy idle timers during long local-model waits.
            # Clients ignore ":" lines; fetch still sees bytes so the stream stays alive.
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=5.0)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                if event is None:
                    break
                if event.get("type") == "ping":
                    yield ": ping\n\n"
                    continue
                yield sse(event)
            yield sse({"type": "end"})

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    # --- background jobs ---
    @app.get("/api/jobs")
    async def jobs():
        return {
            "jobs": [
                {
                    "id": j.id,
                    "name": j.name,
                    "cron": j.cron,
                    "prompt": j.prompt,
                    "enabled": j.enabled,
                    "approved": j.approved,
                    "origin": j.origin,
                    "next_run": j.next_run,
                    "running": j.id in running_jobs,
                }
                for j in agent.memory.jobs()
            ],
            "facts": [{"id": i, "text": t} for i, t in agent.memory.facts()],
            "runs": agent.memory.runs(30),
            "timezone": config.timezone,
        }

    @app.post("/api/jobs")
    async def add_job(body: JobBody):
        if not valid_cron(body.cron.strip()):
            raise HTTPException(400, "not a valid 5-field cron schedule")
        cron = body.cron.strip()
        with agent.memory.transaction():
            job_id = agent.memory.add_job(body.name, cron, body.prompt, next_run_after(cron, config.timezone))
            agent.audit.write(
                "roland",
                "job_created",
                detail={"job_id": job_id, "name": body.name, "cron": cron, "approved": True},
            )
        return {"id": job_id}

    @app.post("/api/jobs/{job_id}/toggle")
    async def toggle_job(job_id: int):
        job = agent.memory.job(job_id)
        if not job:
            raise HTTPException(404, "no such job")
        if not job.approved:
            raise HTTPException(400, "approve the job first")
        if not job.enabled:  # skip runs missed while paused
            agent.memory.set_next_run(job_id, next_run_after(job.cron, config.timezone))
        agent.memory.set_job_enabled(job_id, not job.enabled)
        return {"enabled": not job.enabled}

    @app.post("/api/jobs/{job_id}/run")
    async def run_now(job_id: int):
        job = agent.memory.job(job_id)
        if not job:
            raise HTTPException(404, "no such job")
        if not job.approved:
            raise HTTPException(400, "approve the job first")
        if job_id in running_jobs:
            raise HTTPException(409, "this job is already running")
        if len(running_jobs) >= MAX_PARALLEL_JOBS:
            raise HTTPException(429, "too many jobs are running; try again shortly")
        running_jobs.add(job_id)  # reserved now, so quick double clicks can't start it twice
        task = asyncio.create_task(execute(agent, job))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/approve")
    async def approve_job(job_id: int):
        job = agent.memory.job(job_id)
        if not job:
            raise HTTPException(404, "no such job")
        with agent.memory.transaction():
            agent.memory.set_next_run(job_id, next_run_after(job.cron, config.timezone))
            if not agent.memory.approve_job(job_id):
                raise HTTPException(404, "no such job")
            agent.audit.write("roland", "job_approved", detail={"job_id": job_id})
        return {"ok": True}

    @app.delete("/api/facts/{fact_id}")
    async def delete_fact(fact_id: int):
        with agent.memory.transaction():
            if not agent.memory.forget(fact_id):
                raise HTTPException(404, "no such fact")
            agent.audit.write("roland", "fact_deleted", detail={"fact_id": fact_id})
        return {"ok": True}

    @app.delete("/api/jobs/{job_id}")
    async def delete_job(job_id: int):
        with agent.memory.transaction():
            if not agent.memory.delete_job(job_id):
                raise HTTPException(404, "no such job")
            agent.audit.write("roland", "job_deleted", detail={"job_id": job_id})
        return {"ok": True}

    @app.get("/favicon.ico")
    async def favicon():
        # Real icon avoids Safari briefly flashing another site's tab mark (seen as a
        # "GitHub logo" flash during the thinking→reply transition on Contabo).
        return FileResponse(STATIC / "favicon.svg", media_type="image/svg+xml")

    return app
