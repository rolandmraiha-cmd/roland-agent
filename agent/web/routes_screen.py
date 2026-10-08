"""Screen and sign-in HTTP routes (§6.6, §6.7, §8.2).

`/internal/screen-auth` is what Caddy asks before it lets a request through to noVNC. Core
never carries the screen itself: no keystroke, picture or VNC byte passes through here.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from ..screen import ScreenError
from .auth import Sessions, token_hash
from .middleware import _header


class ScreenSessionBody(BaseModel):
    mode: Literal["watch", "control"]
    signin_id: str | None = Field(default=None, min_length=1, max_length=64)


class ScreenIdBody(BaseModel):
    id: str = Field(min_length=1, max_length=64)


def build_router(agent, sessions: Sessions, cookie: str, static) -> APIRouter:
    router = APIRouter()
    config = agent.config
    screens = agent.screens
    signins = agent.signins

    def login_hash(request: Request) -> str:
        # AuthMiddleware has checked the cookie already; the hash ties the screen to this login.
        return token_hash(request.cookies.get(cookie) or "")

    def need_screen() -> None:
        if not screens.available:
            raise HTTPException(404, "the screen is turned off")

    @router.get("/screen")
    async def screen_page():
        need_screen()
        return FileResponse(static / "screen.html", headers={"Cache-Control": "no-store"})

    @router.post("/api/screen/session")
    async def start_session(request: Request, body: ScreenSessionBody):
        need_screen()
        try:
            session = await screens.start(login_hash(request), body.mode, body.signin_id)
        except ScreenError as error:
            raise HTTPException(error.status, str(error)) from None
        # The one place a VNC password leaves core: this response body, to Roland's own page.
        return {**session, "vnc_password": screens.password_for(session["mode"])}

    @router.post("/api/screen/heartbeat")
    async def heartbeat(request: Request, body: ScreenIdBody):
        need_screen()
        await screens.expire_idle()
        session = screens.touch(body.id, login_hash(request))
        if session is None:
            raise HTTPException(410, "that screen session is over")
        return session

    @router.post("/api/screen/release")
    async def release(request: Request, body: ScreenIdBody):
        need_screen()
        return {"ok": True, "ended": await screens.release(body.id, login_hash(request))}

    @router.get("/api/signin")
    async def list_signins(status: str = "pending"):
        if status != "pending":
            raise HTTPException(400, "invalid status")
        return signins.active()

    async def decide(signin_id: str, action) -> dict:
        try:
            return await action(signin_id)
        except KeyError:
            raise HTTPException(404, "no such sign-in") from None
        except LookupError as error:
            raise HTTPException(409, str(error)) from None

    @router.post("/api/signin/{signin_id}/done")
    async def signin_done(signin_id: str):
        return await decide(signin_id, signins.done)

    @router.post("/api/signin/{signin_id}/cancel")
    async def signin_cancel(signin_id: str):
        return await decide(signin_id, signins.cancel)

    @router.get("/internal/screen-auth")
    async def screen_auth(request: Request, kind: str = "static"):
        """Caddy's forward_auth (§6.6). AuthMiddleware has already refused every peer but
        Caddy. 200 lets the request through to noVNC; anything else is what the visitor gets."""
        if not screens.available or kind not in {"ws", "static"}:
            return Response(status_code=403)
        token = request.cookies.get(cookie)
        if not sessions.valid(token):
            return Response(status_code=401)
        await screens.expire_idle()
        if screens.active_for(token_hash(token)) is None:
            return Response(status_code=403)
        if kind == "ws":
            # A page on another site can open a websocket here with Roland's cookie; its
            # Origin gives it away. Only this site's own pages may connect.
            schemes = ("https",) if config.cookie_secure else ("http", "https")
            origin = _header(request.headers, "origin")
            if not config.agent_host or origin not in {f"{scheme}://{config.agent_host}" for scheme in schemes}:
                return Response(status_code=403)
        return Response(status_code=200)

    return router
