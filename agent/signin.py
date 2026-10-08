"""Sign-in requests (§6.7): the agent asks, Roland signs in himself on the screen.

`request_signin` opens the page, hands the browser to Roland and waits. Only the Done button
(`POST /api/signin/{id}/done`), Cancel, the Stop button or the timeout end the wait; a chat
message never does. Credentials never pass through core: Roland types them into noVNC.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from . import policy_browser
from .browser_client import ERROR_TEXT, BrowserError

if TYPE_CHECKING:
    from .audit import Audit, NullAudit
    from .config import Config
    from .memory import Memory
    from .screen import Screens

ACTIVE = frozenset({"pending", "in_progress"})
# How long the tool waits, once a sign-in is decided, for the browser to be back with the agent.
HANDOVER_WAIT_S = 30.0
UNAVAILABLE = "Error: asking Roland to sign in is turned off. Tell him which site needs a sign-in."
ONLY_IN_CHAT = (
    "Error: request_signin only works in a chat with Roland. Say which site needs a sign-in and stop."
)
NEEDS_APPROVAL = (
    "Error: opening that address needs Roland's approval. Open it with browser_open first, "
    "then call request_signin with the address of the page that is showing."
)
STOPPED = "Not done: the run was stopped."
# What the model is told when Roland presses the button. The button only says he stopped; the
# model once answered "successfully signed in" from it while the sign-in form was still
# showing (Contabo, 2026-10-08), so the page as it is now is handed over with it.
DONE = 'Roland pressed "I\'m done" for {site}. That doesn\'t prove he is signed in.'
DONE_PAGE = " This is the page now; go by what it shows:\n"
DONE_LOOK = " Take a snapshot and go by what the page shows."


def signin_public(row: dict) -> dict:
    """What the API and the `signin_required` event carry (§8.5). Never anything typed."""
    return {
        "id": row["id"],
        "site": row["site"],
        "url": row["url"],
        "reason": row.get("reason"),
        "status": row["status"],
        "created": row["created"],
        "expires": row["expires"],
        "chat_id": row.get("chat_id"),
    }


def _bad_url(url: str) -> str | None:
    """Why this address can't be a sign-in page, or None when it can."""
    if not url or len(url) > 2048:
        return "give the address of the sign-in page (http or https)"
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return "that address can't be read"
    if parts.scheme not in {"http", "https"} or not host or len(host) > 253:
        return "give the address of the sign-in page (http or https)"
    if parts.username is not None or parts.password is not None:
        return "the address must not carry a user name or password"
    return None


class SignIns:
    def __init__(self, memory: Memory, audit: Audit | NullAudit, config: Config, screens: Screens):
        self.memory = memory
        self.audit = audit
        self.config = config
        self.screens = screens
        self._waiters: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    @property
    def minutes(self) -> int:
        return max(1, self.config.signin_timeout_min)

    def active(self, chat_id: int | None = None) -> list[dict]:
        return [signin_public(row) for row in self.memory.active_signin_requests(chat_id)]

    def reset_on_startup(self) -> int:
        """A restart ends every wait: nothing is listening for the Done button any more."""
        count = 0
        for row in self.memory.active_signin_requests():
            with self.memory.transaction():
                if self.memory.set_signin_status(row["id"], "cancelled", expected_status=row["status"]):
                    count += 1
                    self._audit("system", row, "cancelled", reason="restart")
        return count

    def _audit(self, actor: str, row: dict, status: str, **extra) -> None:
        self.audit.write(
            actor,
            "signin_resolved",
            run_id=row.get("run_id"),
            chat_id=row.get("chat_id"),
            decision=status,
            detail={"signin_id": row["id"], "site": row["site"], **extra},
        )

    async def request(
        self, ctx, url: str, reason: str = "", *, page_now: Callable[[], Awaitable[str]] | None = None,
    ) -> str:
        """The `request_signin` tool: returns the line the model is told when the wait ends.
        `page_now` reads the page once Roland is done; what it returns goes into that line."""
        run = getattr(ctx, "run", None)
        if not self.screens.available:
            return UNAVAILABLE
        if run is None or getattr(run, "origin", None) != "chat" or getattr(run, "chat_id", None) is None:
            return ONLY_IN_CHAT
        if run.stopped:
            return STOPPED
        problem = _bad_url(url)
        if problem:
            return f"Error: {problem}."
        browser = self.screens.browser
        async with self._lock:
            if self.memory.active_signin_requests():
                return "Error: another sign-in is already waiting for Roland."
            if self.screens.roland_has_browser():
                return f"Error: {ERROR_TEXT['user_mode']}"
            try:
                status = await browser.status()
                if status.get("mode") == "user":
                    return f"Error: {ERROR_TEXT['user_mode']}"
                if status.get("url") != url:
                    # The same rule as browser_open, so this is no way around its approval.
                    verdict = policy_browser.classify_open(url)
                    if verdict.risk == "forbidden":
                        return f"Error: {verdict.why}."
                    if verdict.risk != "safe":
                        return NEEDS_APPROVAL
                    answer = await browser.navigate(url)
                    if answer.get("blocked"):
                        return "Error: the browser refused that address (private, local or not a web page)."
            except BrowserError as error:
                return f"Error: {error}"
            expires = time.time() + self.minutes * 60
            with self.memory.transaction():
                signin_id = self.memory.add_signin_request(
                    run.run_id, url, expires, chat_id=run.chat_id, reason=reason or None,
                )
                row = self.memory.signin_request(signin_id)
                assert row is not None
                self.audit.write(
                    "agent",
                    "signin_requested",
                    run_id=run.run_id,
                    chat_id=run.chat_id,
                    tool="request_signin",
                    detail={"signin_id": signin_id, "site": row["site"]},
                )
            future: asyncio.Future = asyncio.get_running_loop().create_future()
            self._waiters[signin_id] = future
            # From the insert on, BrowserClient refuses the agent's own calls. browserd is told too.
            if not await self.screens.sync_browser():
                await self._close(row, "cancelled", "system", reason="browser_unreachable")
                return "Error: the browser isn't reachable right now."
        await run.events.put({"type": "signin_required", "signin": signin_public(row)})
        try:
            self.memory.add_event(
                run.chat_id, "signin", f"Sign-in needed: {row['site']}",
                {"signin_id": signin_id, "status": "pending"}, run.run_id,
            )
        except (TypeError, ValueError, OSError):
            pass  # the timeline row is best-effort; the card already went out
        try:
            while not future.done():
                if run.stopped:
                    break
                current = self.memory.signin_request(signin_id)
                if current is None or current["status"] not in ACTIVE:
                    # Decided, but `_close` may still be cutting Roland's screen and handing
                    # the browser back, which takes a moment with a real browser. It wakes
                    # the future last; returning on the stored status alone would let the
                    # model's next call find the browser still locked.
                    try:
                        await asyncio.wait_for(asyncio.shield(future), timeout=HANDOVER_WAIT_S)
                    except TimeoutError:
                        pass
                    break
                if time.time() >= current["expires"]:
                    break
                try:
                    await asyncio.wait_for(asyncio.shield(future), timeout=0.2)
                except TimeoutError:
                    continue
        finally:
            current = self.memory.signin_request(signin_id) or row
            if current["status"] in ACTIVE:
                # Nobody pressed a button: the Stop button, a cancelled task or the clock.
                expired = not run.stopped and time.time() >= current["expires"]
                await self._close(
                    current, "expired" if expired else "cancelled", "system" if expired else "roland",
                    reason="timeout" if expired else "stopped",
                )
            self._waiters.pop(signin_id, None)
            final = (self.memory.signin_request(signin_id) or current)["status"]
            await run.events.put({"type": "signin_resolved", "id": signin_id, "status": final})
        if final == "done":
            page = await page_now() if page_now is not None else ""
            return DONE.format(site=row["site"]) + (DONE_PAGE + page if page else DONE_LOOK)
        if final == "expired":
            return f"Roland didn't finish signing in within {self.minutes} minutes."
        if run.stopped:
            return STOPPED
        return "Roland cancelled the sign-in."

    async def done(self, signin_id: str) -> dict:
        """Roland pressed "I'm done". The only thing that makes a sign-in count as finished."""
        return await self._decide(signin_id, "done")

    async def cancel(self, signin_id: str) -> dict:
        return await self._decide(signin_id, "cancelled")

    async def _decide(self, signin_id: str, status: str) -> dict:
        row = self.memory.signin_request(signin_id)
        if row is None:
            raise KeyError("no such sign-in")
        if row["status"] not in ACTIVE:
            raise LookupError("that sign-in is no longer waiting")
        if not await self._close(row, status, "roland"):
            raise LookupError("that sign-in is no longer waiting")
        return {"status": status}

    async def _close(self, row: dict, status: str, actor: str, **extra) -> bool:
        """Record the outcome, end Roland's screen, give the browser back, then wake the tool.
        In that order: the model's next call must find the browser its own again."""
        with self.memory.transaction():
            current = self.memory.signin_request(row["id"])
            if current is None or current["status"] not in ACTIVE:
                return False
            if status == "done" and current["status"] == "pending":
                # Done without the screen ever opened: he was signed in already.
                self.memory.set_signin_status(row["id"], "in_progress")
                current = dict(current, status="in_progress")
            if not self.memory.set_signin_status(row["id"], status, expected_status=current["status"]):
                return False
            self._audit(actor, row, status, **extra)
        try:
            await self.screens.end_all(f"signin_{status}")
        finally:
            future = self._waiters.get(row["id"])
            if future is not None and not future.done():
                future.set_result(status)
        return True
