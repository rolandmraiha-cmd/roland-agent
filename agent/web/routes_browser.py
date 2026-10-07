"""Read browser status and fetch a user-requested thumbnail."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response

from ..browser_client import PNG_MAGIC, BrowserError, BrowserLocked

STATUS_TIMEOUT_S = 2.0
MAX_TABS = 20


def _text(value: object, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def _status_payload(answer: dict) -> dict:
    """Expose only bounded, typed browserd status fields."""
    mode = answer.get("mode")
    if not isinstance(mode, str) or mode not in {"agent", "user"}:
        mode = None
    tabs = []
    raw_tabs = answer.get("tabs")
    if isinstance(raw_tabs, list):
        for tab in raw_tabs[:MAX_TABS]:
            if isinstance(tab, dict):
                tabs.append(
                    {
                        "id": _text(tab.get("id"), 120),
                        "url": _text(tab.get("url"), 300),
                        "title": _text(tab.get("title"), 120),
                        "active": tab.get("active") is True,
                    }
                )
    return {
        "enabled": True,
        "reachable": True,
        "mode": mode,
        "url": _text(answer.get("url"), 300),
        "title": _text(answer.get("title"), 120),
        "tabs": tabs,
    }


async def browser_status(agent) -> dict:
    """Keep status polling short even when the browser service is down."""
    browser = agent.ctx.browser
    if browser is None:
        return {"enabled": False}
    try:
        answer = await asyncio.wait_for(browser.status(), timeout=STATUS_TIMEOUT_S)
    except (BrowserError, TimeoutError):
        return {"enabled": True, "reachable": False}
    if not isinstance(answer, dict):
        return {"enabled": True, "reachable": False}
    return _status_payload(answer)


def build_router(agent) -> APIRouter:
    router = APIRouter()

    @router.get("/api/browser/status")
    async def status():
        return await browser_status(agent)

    @router.post("/api/browser/screenshot")
    async def screenshot():
        browser = agent.ctx.browser
        if browser is None:
            return JSONResponse({"error": "browser disabled"}, status_code=404)
        try:
            data = await browser.screenshot(full_page=False)
        except BrowserLocked:
            return JSONResponse({"error": "user_mode"}, status_code=423)
        except BrowserError:
            return JSONResponse({"error": "browser unavailable"}, status_code=503)
        if not isinstance(data, bytes) or not data.startswith(PNG_MAGIC):
            return JSONResponse({"error": "browser unavailable"}, status_code=503)
        return Response(
            data,
            media_type="image/png",
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"},
        )

    return router
