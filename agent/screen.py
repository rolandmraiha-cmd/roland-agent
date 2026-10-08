"""Roland's live screen of the agent's browser (§6.6): screen sessions, and who has the browser.

There is one Chromium. Roland can watch it (the agent keeps working) or take control of it
(the agent is locked out). A waiting sign-in (§6.7, `signin.py`) locks the agent out as well.
This module keeps the session records, decides from them whether Roland has the browser, and
tells browserd. It never sees a keystroke: the screen travels from Caddy to noVNC to x11vnc.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from .browser_client import BrowserError

if TYPE_CHECKING:
    from .audit import Audit, NullAudit
    from .browser_client import BrowserClient
    from .config import Config
    from .memory import Memory

MODES = frozenset({"watch", "control"})
WS_PATH = "/screen/websockify"


class ScreenError(Exception):
    """A screen request that can't be met. `status` is the HTTP status for the API."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


class Screens:
    def __init__(
        self, memory: Memory, audit: Audit | NullAudit, config: Config, browser: BrowserClient | None,
    ):
        self.memory = memory
        self.audit = audit
        self.config = config
        self.browser = browser
        self._lock = asyncio.Lock()
        # Set when screen connections should be cut but browserd couldn't be told; tick() does it.
        self._disconnect_due = False

    @property
    def available(self) -> bool:
        """The screen needs its own switch and the browser it shows."""
        return bool(self.config.screen_enabled and self.browser is not None)

    @property
    def idle_seconds(self) -> float:
        return max(1, self.config.screen_session_idle_min) * 60

    def roland_has_browser(self) -> bool:
        """True while Roland controls the browser or a sign-in is waiting for him. The agent
        must not read, act or take pictures then; BrowserClient asks this before every call."""
        if not self.available:
            return False
        if self.memory.active_signin_requests():
            return True
        return any(row["mode"] == "control" for row in self.memory.screen_sessions())

    async def sync_browser(self) -> bool:
        """Put browserd in the mode the records call for. False when it couldn't be reached;
        the caller decides what that means, and `tick` tries again."""
        if self.browser is None:
            return False
        try:
            await self.browser.user_mode(self.roland_has_browser())
        except BrowserError:
            return False
        return True

    def password_for(self, mode: str) -> str:
        """Watching gets the view-only password, which x11vnc itself enforces."""
        return self.config.vnc_password if mode == "control" else self.config.vnc_view_password

    def active_for(self, login_hash: str) -> dict | None:
        for row in self.memory.screen_sessions():
            if row["session_hash"] == login_hash:
                return row
        return None

    def public(self, row: dict) -> dict:
        return {
            "id": row["id"],
            "mode": row["mode"],
            "ws_path": WS_PATH,
            "signin_id": row.get("signin_id"),
            "expires": row["last_seen"] + self.idle_seconds,
        }

    async def start(self, login_hash: str, mode: str, signin_id: str | None = None) -> dict:
        """Open the one screen session, ending any other first. Control hands the browser to
        Roland before this returns, so the agent is out before he can connect."""
        if not self.available:
            raise ScreenError("the screen is turned off", 404)
        if mode not in MODES:
            raise ScreenError("mode must be watch or control", 400)
        async with self._lock:
            signin = None
            if signin_id is not None:
                if mode != "control":
                    raise ScreenError("a sign-in needs the control screen", 400)
                signin = self.memory.signin_request(signin_id)
                if signin is None or signin["status"] not in {"pending", "in_progress"}:
                    raise ScreenError("that sign-in is no longer waiting", 409)
            await self._end(self.memory.screen_sessions(), "replaced", sync=False)
            with self.memory.transaction():
                screen_id = self.memory.add_screen_session(login_hash, mode, signin_id=signin_id)
                if signin is not None and signin["status"] == "pending":
                    self.memory.set_signin_status(signin_id, "in_progress")
                self.audit.write("roland", "screen_session_start", detail={"mode": mode})
            if not await self.sync_browser():
                # Never hand out a password for a browser the agent may still be driving.
                await self._end([self.memory.screen_session(screen_id)], "failed", sync=False)
                raise ScreenError("the browser isn't reachable right now", 503)
            row = self.memory.screen_session(screen_id)
            assert row is not None
            return self.public(row)

    def touch(self, screen_id: str, login_hash: str) -> dict | None:
        """The screen page's heartbeat. None when that session is over."""
        if not self.memory.touch_screen_session(screen_id, login_hash):
            return None
        row = self.memory.screen_session(screen_id)
        return self.public(row) if row else None

    async def release(self, screen_id: str, login_hash: str) -> bool:
        async with self._lock:
            row = self.memory.screen_session(screen_id)
            if row is None or row["ended"] is not None or row["session_hash"] != login_hash:
                return False
            await self._end([row], "released")
            return True

    async def end_for_login(self, login_hash: str, reason: str = "logout") -> int:
        async with self._lock:
            rows = [row for row in self.memory.screen_sessions() if row["session_hash"] == login_hash]
            return await self._end(rows, reason) if rows else 0

    async def end_all(self, reason: str) -> int:
        """End every session, cut every screen connection and give the browser back when
        nothing else holds it. Used when a sign-in finishes and when core starts."""
        async with self._lock:
            return await self._end(self.memory.screen_sessions(), reason, always_disconnect=True)

    def reset_on_startup(self) -> int:
        """A restart ends every session: its page can no longer be told apart from a stale
        one. Records only, so starting never waits for browserd; tick() cuts the connections
        and hands the browser back."""
        ended = self._finish(self.memory.screen_sessions(), "restart")
        self._disconnect_due = True
        return ended

    async def expire_idle(self) -> int:
        async with self._lock:
            limit = time.time() - self.idle_seconds
            rows = [row for row in self.memory.screen_sessions() if row["last_seen"] <= limit]
            return await self._end(rows, "expired") if rows else 0

    async def tick(self) -> None:
        """Housekeeping, run every few seconds while the screen is on: end idle sessions and
        correct browserd if it disagrees with the records (it forgets user mode on a restart,
        and core may have been unable to reach it when a session ended)."""
        if not self.available:
            return
        await self.expire_idle()
        async with self._lock:
            try:
                if self._disconnect_due:
                    await self.browser.vnc_disconnect()  # type: ignore[union-attr]
                    self._disconnect_due = False
                status = await self.browser.status()  # type: ignore[union-attr]
            except BrowserError:
                return
            wanted = "user" if self.roland_has_browser() else "agent"
            if status.get("mode") != wanted:
                await self.sync_browser()

    async def _end(
        self, rows: list[dict | None], reason: str, *, sync: bool = True, always_disconnect: bool = False,
    ) -> int:
        """Mark sessions over, then cut the screen connections, then (last) let the agent back
        in: Roland must not be able to go on typing into a browser the agent is driving."""
        ended = self._finish(rows, reason)
        if (ended or always_disconnect) and self.browser is not None:
            try:
                await self.browser.vnc_disconnect()
            except BrowserError:
                self._disconnect_due = True  # tick() tries again
            if sync:
                await self.sync_browser()
        return ended

    def _finish(self, rows: list[dict | None], reason: str) -> int:
        """End the records and audit each end with its mode and how long it lasted, no more."""
        ended = 0
        now = time.time()
        for row in rows:
            if row is None or row.get("ended") is not None:
                continue
            with self.memory.transaction():
                if not self.memory.finish_screen_session(row["id"]):
                    continue
                self.audit.write(
                    "system" if reason in {"expired", "restart", "failed"} else "roland",
                    "screen_session_end",
                    detail={"mode": row["mode"], "duration_s": max(0, round(now - row["started"])), "reason": reason},
                )
            ended += 1
        return ended
