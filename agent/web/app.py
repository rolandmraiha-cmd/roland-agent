"""The web chat page and its API, served by the agent itself."""

from __future__ import annotations

import asyncio
import html
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..core import Agent, sse
from ..schedule import next_run_after, valid_cron
from ..scheduler import execute, scheduler_loop
import hmac

from fastapi.middleware.trustedhost import TrustedHostMiddleware

from .auth import COOKIE, LoginLimiter, Sessions, csrf_token, password_ok

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


class JobBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    cron: str = Field(min_length=9, max_length=100)
    prompt: str = Field(min_length=1, max_length=5000)


def create_app(agent: Agent, run_scheduler: bool = True) -> FastAPI:
    config = agent.config
    sessions = Sessions(agent.memory, config.session_days)
    limiter = LoginLimiter()
    busy: set[int] = set()
    tasks: set[asyncio.Task] = set()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        loop_task = asyncio.create_task(scheduler_loop(agent)) if run_scheduler else None
        yield
        if loop_task:
            loop_task.cancel()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def logged_in(request: Request) -> bool:
        return sessions.valid(request.cookies.get(COOKIE))

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        # CSRF, two layers: every state-changing request must come from this site's own page
        # (Origin check), and once logged in it must also carry the session's CSRF token.
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin") or request.headers.get("referer")
            host = request.headers.get("host", "")
            if not origin or urlparse(origin).netloc != host:
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
        ip = request.client.host if request.client else "?"
        wait = limiter.locked(ip)
        if wait:
            return login_page(f"Too many wrong passwords. Try again in {int(wait // 60) + 1} min.", 429)
        form = await request.form()
        given = str(form.get("password", ""))[:1024]
        if not await asyncio.to_thread(password_ok, given, config.password_hash):
            limiter.failed(ip)
            await asyncio.sleep(1)  # slows down guessing
            return login_page("Wrong password.", 401)
        limiter.succeeded(ip)
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
                      "enabled": j.enabled, "next_run": j.next_run} for j in agent.memory.jobs()],
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
        if not job.enabled:  # skip runs missed while paused
            agent.memory.set_next_run(job_id, next_run_after(job.cron, config.timezone))
        agent.memory.set_job_enabled(job_id, not job.enabled)
        return {"enabled": not job.enabled}

    @app.post("/api/jobs/{job_id}/run")
    async def run_now(job_id: int):
        job = agent.memory.job(job_id)
        if not job:
            raise HTTPException(404, "no such job")
        task = asyncio.create_task(execute(agent, job))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
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
