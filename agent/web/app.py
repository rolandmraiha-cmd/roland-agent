"""The web chat page and its API, served by the agent itself."""

from __future__ import annotations

import asyncio
import hmac
import html
import ipaddress
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from ..config import trusted_proxy_networks
from ..core import Agent, sse
from ..schedule import next_run_after, valid_cron
from ..scheduler import MAX_PARALLEL_JOBS, execute, running_jobs, scheduler_loop
from .auth import (
    COOKIE,
    MAX_WAITING,
    LoginGate,
    LoginLimiter,
    Sessions,
    client_key,
    csrf_token,
    password_ok,
)

STATIC = Path(__file__).parent / "static"

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; object-src 'none'; "
                               "base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # "no-referrer" would make browsers send "Origin: null" on the login form and break the
    # Origin check; "same-origin" sends it to this site only.
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


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


def _strip_port(hop: str) -> str:
    """'1.2.3.4:5678' becomes '1.2.3.4' and '[2001:db8::1]:443' becomes '2001:db8::1'."""
    hop = hop.strip()
    if hop.startswith("["):
        return hop[1:hop.find("]")] if "]" in hop else hop
    if hop.count(":") == 1:
        return hop.split(":")[0]
    return hop


class ProxyHeaders:
    """Takes the visitor's IP from X-Forwarded-For, but only when the request comes straight
    from a trusted reverse proxy (FORWARDED_ALLOW_IPS). Anyone else's header is ignored, and a
    warning is logged once, since a forgotten setting makes every visitor look like the proxy and
    share one login lockout."""

    def __init__(self, app, trusted: tuple[str, ...]):
        self.app = app
        self.nets = trusted_proxy_networks(trusted)
        self.warned = False
        self.warned_all_trusted = False

    def trusted(self, host: str) -> bool:
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return False
        return any(ip in n for n in self.nets)

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket") and scope.get("client"):
            host, port = scope["client"]
            # Several X-Forwarded-For lines count as one list, in order.
            fwd = ",".join(v.decode("latin-1") for k, v in scope["headers"]
                           if k == b"x-forwarded-for")
            if fwd and self.trusted(host):
                # The rightmost address not added by a trusted proxy is the real visitor;
                # anything further left could have been typed by the visitor.
                hops = [_strip_port(h) for h in fwd.split(",") if h.strip()]
                visitor = next((h for h in reversed(hops) if not self.trusted(h)), None)
                if visitor is not None:
                    host = visitor
                elif hops and not self.warned_all_trusted:
                    self.warned_all_trusted = True
                    log.warning("Every X-Forwarded-For hop is trusted; keeping direct peer %s.", host)
                proto = next((v.decode("latin-1") for k, v in scope["headers"]
                              if k == b"x-forwarded-proto"), None)
                scope = dict(scope, client=(host, port))
                if proto in ("http", "https") and scope["type"] == "http":
                    scope["scheme"] = proto
            elif fwd and not self.warned:
                self.warned = True
                log.warning("Got X-Forwarded-For from %s, which isn't in FORWARDED_ALLOW_IPS, so "
                            "it was ignored. If that's your reverse proxy, add its IP there; "
                            "otherwise every visitor shares one login lockout.", host)
        await self.app(scope, receive, send)


def create_app(agent: Agent, run_scheduler: bool = True) -> FastAPI:
    config = agent.config
    sessions = Sessions(agent.memory, config.session_days, config.idle_hours, config.password_hash)
    limiter = LoginLimiter()
    gate = LoginGate()
    busy: set[int] = set()
    tasks: set[asyncio.Task] = set()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if run_scheduler and (n := agent.memory.fail_unfinished_runs()):
            log.info("marked %s unfinished job run(s) as failed", n)
        loop_task = asyncio.create_task(scheduler_loop(agent)) if run_scheduler else None
        try:
            yield
        finally:
            # Await cancellation so jobs can finish their failure records and release slots.
            pending = list(tasks)
            if loop_task:
                pending.append(loop_task)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def logged_in(request: Request) -> bool:
        return sessions.valid(request.cookies.get(COOKIE))

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        # CSRF, two layers: every state-changing request must come from this site's own page
        # (Origin check), and once logged in it must also carry the session's CSRF token.
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = urlparse(request.headers.get("origin") or request.headers.get("referer") or "")
            host = request.headers.get("host", "")
            # With secure cookies (online, behind HTTPS) only an https:// origin is accepted.
            schemes = {"https"} if config.cookie_secure else {"http", "https"}
            if origin.scheme not in schemes or origin.netloc != host:
                return JSONResponse({"error": "bad origin"}, status_code=403)
            token = request.cookies.get(COOKIE)
            if path != "/login" and token:
                sent = request.headers.get("x-csrf-token", "")
                if not hmac.compare_digest(sent, csrf_token(token)):
                    return JSONResponse({"error": "bad csrf token"}, status_code=403)
        public = path in {"/login", "/favicon.ico"} or path.startswith("/static/")
        if not public and not logged_in(request):
            if path.startswith("/api/"):
                return JSONResponse({"error": "not logged in"}, status_code=401)
            return RedirectResponse("/login", status_code=303)
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        if path.startswith("/api/") or path in {"/", "/login"}:
            response.headers["Cache-Control"] = "no-store"
        return response

    if config.allowed_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=config.allowed_hosts)
    app.add_middleware(ProxyHeaders, trusted=config.trusted_proxies)  # outermost: runs first

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # --- login ---
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

        def locked_page(wait: float) -> HTMLResponse:
            return login_page(
                f"Too many wrong passwords. Try again in {int(wait // 60) + 1} min.", 429)

        # A locked address is turned away before it can take a place in the queue.
        if wait := limiter.locked(ip):
            return locked_page(wait)
        if ip in gate.inflight:
            return login_page("A login from your address is already being checked.", 429)
        if gate.waiting >= MAX_WAITING:
            return login_page("The login is busy. Try again in a moment.", 429)
        gate.inflight.add(ip)
        try:
            gate.waiting += 1
            try:
                # Check the lockout, verify and record the result as one step, one login at a
                # time. Only that happens under the lock; the slowdown runs after releasing it.
                async with gate.lock:
                    if wait := limiter.locked(ip):
                        return locked_page(wait)
                    ok = await asyncio.to_thread(password_ok, given, config.password_hash)
                    if ok:
                        limiter.succeeded(ip)
                    else:
                        limiter.failed(ip)
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
        response.set_cookie(COOKIE, sessions.create(), max_age=config.session_days * 86400,
                            httponly=True, secure=config.cookie_secure, samesite="strict", path="/")
        return response

    @app.post("/logout")
    async def logout(request: Request):
        sessions.end(request.cookies.get(COOKIE))
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    # --- chats ---
    @app.get("/api/status")
    async def status(request: Request):
        return {"name": config.agent_name, "model": config.model_name,
                "csrf": csrf_token(request.cookies.get(COOKIE, "")),
                "calls_left": agent.calls_left(), "daily_limit": config.daily_call_limit,
                "shell": agent.allow_shell}

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
        return {"messages": agent.memory.messages(chat_id), "busy": chat_id in busy}

    @app.delete("/api/chats/{chat_id}")
    async def delete_chat(chat_id: int):
        if chat_id in busy:
            raise HTTPException(409, "the agent is still answering in this chat")
        need_chat(chat_id)
        agent.memory.delete_chat(chat_id)
        return {"ok": True}

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
            except Exception as e:
                await queue.put({"type": "error", "message": f"{type(e).__name__}: {e}"})
            finally:
                busy.discard(chat_id)
                await queue.put(None)

        task = asyncio.create_task(produce())
        tasks.add(task)
        task.add_done_callback(tasks.discard)

        async def stream():
            while (event := await queue.get()) is not None:
                yield sse(event)
            yield sse({"type": "end"})

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"X-Accel-Buffering": "no"})

    # --- background jobs ---
    @app.get("/api/jobs")
    async def jobs():
        return {
            "jobs": [{"id": j.id, "name": j.name, "cron": j.cron, "prompt": j.prompt,
                      "enabled": j.enabled, "approved": j.approved, "origin": j.origin,
                      "next_run": j.next_run,
                      "running": j.id in running_jobs} for j in agent.memory.jobs()],
            "facts": [{"id": i, "text": t} for i, t in agent.memory.facts()],
            "runs": agent.memory.runs(30),
            "timezone": config.timezone,
        }

    @app.post("/api/jobs")
    async def add_job(body: JobBody):
        if not valid_cron(body.cron.strip()):
            raise HTTPException(400, "not a valid 5-field cron schedule")
        cron = body.cron.strip()
        job_id = agent.memory.add_job(body.name, cron, body.prompt,
                                      next_run_after(cron, config.timezone))
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
        agent.memory.set_next_run(job_id, next_run_after(job.cron, config.timezone))
        agent.memory.approve_job(job_id)
        return {"ok": True}

    @app.delete("/api/facts/{fact_id}")
    async def delete_fact(fact_id: int):
        if not agent.memory.forget(fact_id):
            raise HTTPException(404, "no such fact")
        return {"ok": True}

    @app.delete("/api/jobs/{job_id}")
    async def delete_job(job_id: int):
        if not agent.memory.delete_job(job_id):
            raise HTTPException(404, "no such job")
        return {"ok": True}

    @app.get("/favicon.ico")
    async def favicon():
        return Response(status_code=204)

    return app
