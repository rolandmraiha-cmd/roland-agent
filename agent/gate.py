"""Confirmation gate: tool policies, approval lifecycle, and per-run state (§9)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .audit import Audit, NullAudit
    from .config import Config
    from .memory import Memory
    from .tools import ToolContext

CONFIRM_CATEGORIES = frozenset({"payment", "message", "public_post", "delete"})

def action_key(name: str, args: dict, summary: str) -> tuple[str, str, str]:
    """What makes two gated calls "the same action" for the rejection memory.

    The summary names the element and site but leaves out or cuts arguments (an overwrite's
    content, typed text), so the arguments that matter count too. Left out: the model's
    `reason`, `_`-keys pinned by classifiers, and a browser `ref`, which a fresh snapshot may
    renumber for the same element.
    """
    kept = {
        key: value
        for key, value in args.items()
        if not key.startswith("_") and key != "reason" and not (key == "ref" and name.startswith("browser_"))
    }
    return name, summary, json.dumps(kept, sort_keys=True, ensure_ascii=False, default=str)


# Returned instead of a new card when the run asks again for something Roland rejected.
# The chat loop ends tool use at the first rejection (agent/core.py), so it never gets this
# far there; the gate keeps the check for any other caller.
ALREADY_REJECTED = (
    "Roland already rejected this in this conversation turn, so it wasn't asked again. "
    "Don't try it again; tell Roland it wasn't done."
)

# Reserved argument name. Only a classifier may fill it (through Decision.pinned): call_tool
# drops anything the model put there. It carries facts that must stay bound to the approved
# action, such as the fingerprint of the page element Roland was shown.
PIN_KEY = "_pin"
CARD_SCREENSHOT_TIMEOUT_S = 15


class Risk(StrEnum):
    SAFE = "safe"
    GATED = "gated"
    FORBIDDEN = "forbidden"


@dataclass(frozen=True)
class Decision:
    risk: Risk
    category: str = "other"
    reason: str = ""
    summary: str | None = None
    details: dict | None = None
    # Code-made values stored with the args under PIN_KEY and handed to the tool handler.
    pinned: dict | None = None
    # Optional: called with the new approval id, returns a workspace-relative PNG path to show
    # on the approval card (browser actions). Best-effort; the card appears without it.
    card_screenshot: Callable[[str], Awaitable[str | None]] | None = None


@dataclass
class ToolPolicy:
    classify: Callable[[ToolContext, dict], Awaitable[Decision]]
    taints: bool = False
    in_jobs: bool = True


@dataclass
class Outcome:
    approved: bool
    message: str = ""
    args: dict | None = None
    approval_id: str | None = None


@dataclass
class RunState:
    run_id: str
    chat_id: int | None = None
    job_id: int | None = None
    origin: str = "chat"
    tainted: bool = False
    events: asyncio.Queue = field(default_factory=asyncio.Queue)
    pending_approval_id: str | None = None
    stopped: bool = False
    # Page elements whose unapproved action tried to submit a form (keys from
    # tools_browser._target). The next action on the same element is gated instead of being
    # blocked again (§6.5 POST-navigation guard).
    blocked_submissions: set[str] = field(default_factory=set)
    # action_key() of every approval Roland rejected in this run. The same action gets no
    # new card from the gate. The loop goes further: a non-empty set ends tool use for the
    # turn, so a revised or different action waits for Roland's next message too.
    rejected_actions: set[tuple[str, str, str]] = field(default_factory=set)


def _safe(_ctx: ToolContext, _args: dict) -> Awaitable[Decision]:
    async def go() -> Decision:
        return Decision(Risk.SAFE)

    return go()


def _safe_policy(*, taints: bool = False, in_jobs: bool = True) -> ToolPolicy:
    return ToolPolicy(classify=_safe, taints=taints, in_jobs=in_jobs)


async def _classify_write_file(ctx: ToolContext, args: dict) -> Decision:
    from .tools_files import workspace_from_ctx
    from .workspace import WorkspaceError, normalize

    path = str(args.get("path", ""))
    append = bool(args.get("append"))
    try:
        normalize(path)
    except (WorkspaceError, ValueError) as error:
        return Decision(Risk.FORBIDDEN, "other", reason=str(error))
    ws = workspace_from_ctx(ctx)
    exists = False
    try:
        info = ws.info(path)
        exists = info["type"] == "file"
    except (FileNotFoundError, WorkspaceError, OSError):
        exists = False
    if append or not exists:
        return Decision(Risk.SAFE, summary=f"Write {path}", details={"path": path, "append": append})
    # Overwrite of an existing file: SAFE only for the agent's own file in this chat, untainted.
    record = None
    try:
        record = ctx.memory.file(path)
    except ValueError:
        record = None
    run = ctx.run
    same_chat = (
        record is not None
        and record.get("origin") == "agent"
        and run is not None
        and run.chat_id is not None
        and record.get("chat_id") == run.chat_id
        and not run.tainted
    )
    details = {"path": path, "append": False, "exists": True}
    if same_chat:
        return Decision(Risk.SAFE, summary=f"Overwrite own file {path}", details=details)
    return Decision(
        Risk.GATED,
        "delete",
        reason="overwriting an existing file",
        summary=f"Overwrite {path}",
        details=details,
    )


async def _classify_delete_file(ctx: ToolContext, args: dict) -> Decision:
    from .tools_files import workspace_from_ctx
    from .workspace import WorkspaceError, normalize

    path = str(args.get("path", ""))
    try:
        normalize(path)
        info = workspace_from_ctx(ctx).info(path)
    except (WorkspaceError, ValueError) as error:
        return Decision(Risk.FORBIDDEN, "other", reason=str(error))
    except (FileNotFoundError, OSError) as error:
        return Decision(Risk.FORBIDDEN, "other", reason=str(error))
    return Decision(
        Risk.GATED,
        "delete",
        reason="deleting a file",
        summary=f"Delete {path}",
        details={"path": path, "size": info["size"], "modified": info["modified"]},
    )


async def _classify_move_file(ctx: ToolContext, args: dict) -> Decision:
    from .tools_files import workspace_from_ctx
    from .workspace import WorkspaceError, normalize

    src = str(args.get("from", args.get("src", "")))
    dst = str(args.get("to", args.get("dst", "")))
    try:
        normalize(src)
        normalize(dst)
    except (WorkspaceError, ValueError) as error:
        return Decision(Risk.FORBIDDEN, "other", reason=str(error))
    ws = workspace_from_ctx(ctx)
    try:
        ws.info(src)
    except (FileNotFoundError, WorkspaceError, OSError) as error:
        return Decision(Risk.FORBIDDEN, "other", reason=str(error))
    dest_exists = False
    try:
        ws.info(dst)
        dest_exists = True
    except (FileNotFoundError, WorkspaceError, OSError):
        dest_exists = False
    details = {"from": src, "to": dst, "replaces": dest_exists}
    if not dest_exists:
        return Decision(Risk.SAFE, summary=f"Move {src} → {dst}", details=details)
    return Decision(
        Risk.GATED,
        "delete",
        reason="replacing an existing file",
        summary=f"Move {src} → {dst} (replace)",
        details=details,
    )


async def _classify_remember(ctx: ToolContext, args: dict) -> Decision:
    fact = str(args.get("fact", "")).strip()
    details = {"fact": fact[:200]}
    if ctx.run and ctx.run.tainted:
        return Decision(
            Risk.GATED,
            "memory",
            reason="run is tainted",
            summary=f"Remember (tainted run): {fact[:80]}",
            details=details,
        )
    return Decision(Risk.SAFE, summary=f"Remember: {fact[:80]}", details=details)


async def _classify_forget(ctx: ToolContext, args: dict) -> Decision:
    fact_id = int(args.get("fact_id", 0) or 0)
    return Decision(
        Risk.GATED,
        "delete",
        reason="forgetting a saved fact",
        summary=f"Forget fact {fact_id}",
        details={"fact_id": fact_id},
    )


async def _classify_cancel_job(ctx: ToolContext, args: dict) -> Decision:
    job_id = int(args.get("job_id", 0) or 0)
    job = ctx.memory.job(job_id)
    details = {"job_id": job_id}
    if job is not None and not job.approved and job.origin == "agent":
        return Decision(
            Risk.SAFE,
            summary=f"Withdraw unapproved job {job_id}",
            details=details,
        )
    return Decision(
        Risk.GATED,
        "delete",
        reason="cancelling an approved or foreign job",
        summary=f"Cancel job {job_id}",
        details=details,
    )


async def _classify_run_shell(ctx: ToolContext, args: dict) -> Decision:
    from .policy_shell import classify_run_shell

    return await classify_run_shell(ctx, args)


def _browser_policy(name: str) -> ToolPolicy:
    """A browser tool whose risk depends on the page (§9.3). Every browser tool taints the run."""

    async def classify(ctx: ToolContext, args: dict) -> Decision:
        from .tools_browser import CLASSIFIERS

        return await CLASSIFIERS[name](ctx, args)

    return ToolPolicy(classify=classify, taints=True)


def _build_policies() -> dict[str, ToolPolicy]:
    return {
        "fetch_url": _safe_policy(taints=True),
        "run_shell": ToolPolicy(classify=_classify_run_shell, taints=True),
        "read_file": _safe_policy(taints=True),
        "write_file": ToolPolicy(classify=_classify_write_file, taints=False),
        "list_files": _safe_policy(taints=False),
        "delete_file": ToolPolicy(classify=_classify_delete_file, taints=False),
        "move_file": ToolPolicy(classify=_classify_move_file, taints=False),
        "file_info": _safe_policy(taints=False),
        "attach_file": _safe_policy(taints=False),
        "remember": ToolPolicy(classify=_classify_remember, taints=False),
        "forget": ToolPolicy(classify=_classify_forget, taints=False),
        "schedule_job": _safe_policy(taints=False, in_jobs=False),
        "list_jobs": _safe_policy(taints=False),
        "cancel_job": ToolPolicy(classify=_classify_cancel_job, taints=False),
        # Browser (M6). Page content is untrusted, so every one of these taints the run.
        "browser_open": _browser_policy("browser_open"),
        "browser_snapshot": _safe_policy(taints=True),
        "browser_screenshot": _safe_policy(taints=True),
        "browser_click": _browser_policy("browser_click"),
        "browser_type": _browser_policy("browser_type"),
        "browser_press": _browser_policy("browser_press"),
        "browser_select": _browser_policy("browser_select"),
        "browser_scroll": _safe_policy(taints=True),
        "browser_back": _safe_policy(taints=True),
        "browser_forward": _safe_policy(taints=True),
        "browser_tabs": _safe_policy(taints=True),
        "browser_switch_tab": _safe_policy(taints=True),
        "browser_close_tab": _safe_policy(taints=True),
        "browser_wait": _safe_policy(taints=True),
        "browser_upload": _browser_policy("browser_upload"),
        "browser_downloads": _safe_policy(taints=True),
        # Screen (M7). Roland signs in himself; nothing from the page comes back with the answer.
        "request_signin": _safe_policy(taints=False, in_jobs=False),
    }


POLICIES: dict[str, ToolPolicy] = _build_policies()


def approval_public(row: dict) -> dict:
    """Shape returned by the API and SSE (§9.5)."""
    args = row.get("args")
    if args is None and row.get("args_json"):
        args = json.loads(row["args_json"])
    screenshot = row.get("screenshot_path")
    return {
        "id": row["id"],
        "tool": row["tool"],
        "category": row["category"],
        "summary": row["summary"],
        "details": row["details"] if isinstance(row["details"], dict) else json.loads(row["details"]),
        "model_reason": row.get("model_reason"),
        "tainted": bool(row.get("tainted")),
        "needs_confirm": bool(row.get("needs_confirm")),
        "args_hash": row["args_hash"],
        "args": args,
        "screenshot_url": f"/api/files/preview?path={screenshot}" if screenshot else None,
        "status": row["status"],
        "created": row["created"],
        "expires": row["expires"],
        "chat_id": row.get("chat_id"),
        "job_id": row.get("job_id"),
        "decision_note": row.get("decision_note"),
    }


class NoApproverGate:
    """Fail-closed stand-in when a ToolContext has no Gate (v1-style unit tests)."""

    async def request(self, ctx: ToolContext, name: str, args: dict, decision: Decision) -> Outcome:
        return Outcome(
            approved=False,
            message="this needs Roland's approval, and approvals aren't available here",
        )


class Gate:
    """Creates approvals, waits for Roland, and hands back the stored args."""

    def __init__(self, memory: Memory, audit: Audit | NullAudit, config: Config):
        self.memory = memory
        self.audit = audit
        self.config = config
        self._waiters: dict[str, asyncio.Future] = {}
        self._run_queues: dict[str, asyncio.Queue] = {}
        self._lock = asyncio.Lock()

    def expire_on_startup(self) -> int:
        """Mark every pending approval expired (restart = not approved)."""
        now = time.time()
        count = self.memory.expire_all_pending_approvals(now)
        if count:
            self.audit.write(
                "system",
                "approval_decided",
                decision="expired",
                detail={"count": count, "reason": "startup"},
            )
        return count

    def expire_due(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        expired_ids = self.memory.expire_approvals_returning_ids(now)
        for approval_id in expired_ids:
            self._resolve_waiter(approval_id, Outcome(False, message=self._expired_message()))
            self.audit.write(
                "system",
                "approval_decided",
                decision="expired",
                detail={"approval_id": approval_id},
            )
        return len(expired_ids)

    def _expired_message(self, minutes: int | None = None) -> str:
        minutes = self.config.approval_timeout_min if minutes is None else minutes
        return f"Roland didn't answer within {minutes} minutes."

    def _timeout_min(self, ctx: ToolContext) -> int:
        if ctx.run and ctx.run.origin == "job":
            return self.config.job_approval_timeout_min
        return self.config.approval_timeout_min

    def _summary(self, name: str, args: dict, decision: Decision) -> str:
        if decision.summary:
            return decision.summary
        if name == "forget":
            return f"Forget fact {args.get('fact_id')}"
        if name == "remember":
            return f"Remember: {str(args.get('fact', ''))[:80]}"
        if name == "cancel_job":
            return f"Cancel job {args.get('job_id')}"
        if name == "write_file":
            return f"Write {args.get('path')}"
        if name == "run_shell":
            return f"Shell: {str(args.get('command', ''))[:120]}"
        return f"{name} needs approval"

    def _details(self, name: str, args: dict, decision: Decision) -> dict:
        if decision.details is not None:
            return decision.details
        # Never put huge blobs in the card; keep a shallow copy of short values.
        out: dict[str, Any] = {}
        for key, value in args.items():
            text = value if isinstance(value, (int, float, bool)) else str(value)
            if isinstance(text, str) and len(text) > 500:
                text = text[:500] + "…"
            out[key] = text
        return out

    async def request(self, ctx: ToolContext, name: str, args: dict, decision: Decision) -> Outcome:
        run = ctx.run
        if run is None:
            return Outcome(False, message="this needs Roland's approval, and approvals aren't available here")
        if run.stopped:
            return Outcome(False, message="the run was stopped.")

        async with self._lock:
            pending = self.memory.count_pending_approvals()
            if pending >= self.config.max_pending_approvals:
                return Outcome(False, message="too many pending approvals")
            if run.pending_approval_id is not None:
                return Outcome(False, message="this run already has a pending approval")

            timeout_min = self._timeout_min(ctx)
            expires = time.time() + timeout_min * 60
            model_reason = str(args.get("reason", ""))[:300] or None
            summary = self._summary(name, args, decision)
            key = action_key(name, args, summary)
            if key in run.rejected_actions:
                self.audit.write(
                    "agent",
                    "approval_repeat_refused",
                    run_id=run.run_id,
                    chat_id=run.chat_id,
                    tool=name,
                    decision="rejected",
                    detail={"category": decision.category, "summary": summary},
                )
                return Outcome(False, message=ALREADY_REJECTED)
            details = self._details(name, args, decision)
            needs_confirm = decision.category in CONFIRM_CATEGORIES
            approval_id = self.memory.add_approval(
                run.run_id,
                name,
                args,
                decision.category,
                summary,
                details,
                expires,
                chat_id=run.chat_id,
                job_id=run.job_id,
                model_reason=model_reason,
                tainted=run.tainted,
                needs_confirm=needs_confirm,
            )
            if decision.card_screenshot is not None:
                await self._attach_card_screenshot(approval_id, decision)
            run.pending_approval_id = approval_id
            self.audit.write(
                "agent",
                "approval_requested",
                run_id=run.run_id,
                chat_id=run.chat_id,
                tool=name,
                decision="gated",
                detail={"approval_id": approval_id, "category": decision.category, "summary": summary},
            )
            row = self.memory.approval(approval_id)
            assert row is not None
            public = approval_public(row)
            await run.events.put({"type": "approval_required", "approval": public})
            if run.chat_id is not None:
                self.memory.add_event(
                    run.chat_id,
                    "approval",
                    summary,
                    {"approval_id": approval_id, "status": "pending"},
                    run.run_id,
                )
            loop = asyncio.get_running_loop()
            future: asyncio.Future = loop.create_future()
            self._waiters[approval_id] = future

        try:
            # Wake periodically so expiry and stop can take effect without a busy loop.
            while not future.done():
                if run.stopped:
                    if self.memory.cancel_approval(approval_id):
                        self._resolve_waiter(
                            approval_id,
                            Outcome(False, message="the run was stopped.", approval_id=approval_id),
                        )
                    break
                row = self.memory.approval(approval_id)
                if row is None:
                    self._resolve_waiter(
                        approval_id, Outcome(False, message="approval is no longer pending", approval_id=approval_id)
                    )
                    break
                if row["status"] != "pending":
                    break
                if time.time() >= row["expires"]:
                    self.expire_due(time.time())
                    break
                try:
                    await asyncio.wait_for(asyncio.shield(future), timeout=0.2)
                except TimeoutError:
                    continue
            if future.done():
                return future.result()
            row = self.memory.approval(approval_id)
            if row and row["status"] == "approved":
                return Outcome(True, args=json.loads(row["args_json"]), approval_id=approval_id)
            if row and row["status"] == "rejected":
                note = row.get("decision_note")
                msg = "Roland rejected this."
                if note:
                    msg += f" Roland's note: {note}"
                return Outcome(False, message=msg, approval_id=approval_id)
            if row and row["status"] == "expired":
                return Outcome(False, message=self._expired_message(timeout_min), approval_id=approval_id)
            if row and row["status"] == "cancelled":
                return Outcome(False, message="the run was stopped.", approval_id=approval_id)
            return Outcome(False, message="approval is no longer pending", approval_id=approval_id)
        finally:
            run.pending_approval_id = None
            self._waiters.pop(approval_id, None)
            final = self.memory.approval(approval_id)
            if final is not None and final["status"] == "rejected":
                run.rejected_actions.add(key)

    async def _attach_card_screenshot(self, approval_id: str, decision: Decision) -> None:
        """Save what the browser shows right now so Roland can see it on the card. A failure
        here (browser busy, disk full, slow page) must never block or approve anything."""
        assert decision.card_screenshot is not None
        try:
            path = await asyncio.wait_for(
                decision.card_screenshot(approval_id), timeout=CARD_SCREENSHOT_TIMEOUT_S,
            )
            if path:
                self.memory.set_approval_screenshot(approval_id, path)
        except Exception:  # noqa: S110 -- best-effort picture; the card still appears
            pass

    def _resolve_waiter(self, approval_id: str, outcome: Outcome) -> None:
        future = self._waiters.get(approval_id)
        if future is not None and not future.done():
            future.set_result(outcome)

    async def approve(self, approval_id: str, args_hash: str, *, confirm: bool = False) -> dict:
        row = self.memory.approval(approval_id)
        if row is None:
            raise KeyError("no such approval")
        if row["status"] != "pending":
            raise LookupError("not pending")
        if row["args_hash"] != args_hash:
            raise LookupError("args_hash mismatch")
        if time.time() >= row["expires"]:
            self.expire_due(time.time())
            raise LookupError("expired")
        if row["needs_confirm"] and not confirm:
            raise PermissionError("needs_confirm")
        ok = self.memory.decide_approval(approval_id, "approved", args_hash, confirm=confirm)
        if not ok:
            raise LookupError("not pending")
        self.audit.write(
            "roland",
            "approval_decided",
            run_id=row["run_id"],
            chat_id=row.get("chat_id"),
            tool=row["tool"],
            decision="approved",
            detail={"approval_id": approval_id},
        )
        stored = json.loads(self.memory.approval(approval_id)["args_json"])  # type: ignore[index]
        outcome = Outcome(True, args=stored, approval_id=approval_id)
        self._resolve_waiter(approval_id, outcome)
        await self._emit_resolved(row, "approved")
        return {"status": "approved"}

    async def reject(self, approval_id: str, *, note: str | None = None, args_hash: str | None = None) -> dict:
        row = self.memory.approval(approval_id)
        if row is None:
            raise KeyError("no such approval")
        if row["status"] != "pending":
            raise LookupError("not pending")
        digest = args_hash or row["args_hash"]
        ok = self.memory.decide_approval(approval_id, "rejected", digest, note=note)
        if not ok:
            raise LookupError("not pending")
        self.audit.write(
            "roland",
            "approval_decided",
            run_id=row["run_id"],
            chat_id=row.get("chat_id"),
            tool=row["tool"],
            decision="rejected",
            detail={"approval_id": approval_id, "note": (note or "")[:500]},
        )
        message = "Roland rejected this."
        if note:
            message += f" Roland's note: {note[:500]}"
        self._resolve_waiter(approval_id, Outcome(False, message=message, approval_id=approval_id))
        await self._emit_resolved(row, "rejected")
        return {"status": "rejected"}

    async def _emit_resolved(self, row: dict, status: str) -> None:
        # Best-effort: chat event pump may already be gone.
        event = {"type": "approval_resolved", "id": row["id"], "status": status}
        queue = self._run_queues.get(row["run_id"])
        if queue is not None:
            await queue.put(event)

    def register_run(self, run: RunState) -> None:
        self._run_queues[run.run_id] = run.events

    def unregister_run(self, run_id: str) -> None:
        self._run_queues.pop(run_id, None)

    async def cancel_run(self, run_id: str) -> int:
        """Cancel pending approvals for a stopped run."""
        rows = self.memory.approvals_for_run(run_id, status="pending")
        count = 0
        for row in rows:
            if self.memory.cancel_approval(row["id"]):
                count += 1
                self._resolve_waiter(
                    row["id"], Outcome(False, message="the run was stopped.", approval_id=row["id"])
                )
                self.audit.write(
                    "roland",
                    "approval_decided",
                    run_id=run_id,
                    chat_id=row.get("chat_id"),
                    tool=row["tool"],
                    decision="cancelled",
                    detail={"approval_id": row["id"]},
                )
                await self._emit_resolved(row, "cancelled")
        return count


def mark_executed(memory: Memory, audit: Audit | NullAudit, approval_id: str, result: str) -> None:
    ok = not result.startswith("Error:")
    memory.finish_approval(approval_id, ok, result)
    row = memory.approval(approval_id)
    if row:
        audit.write(
            "agent",
            "tool_result",
            run_id=row.get("run_id"),
            chat_id=row.get("chat_id"),
            tool=row.get("tool"),
            decision="executed" if ok else "failed",
            detail={
                "approval_id": approval_id,
                "digest": hashlib.sha256(result.encode("utf-8")).hexdigest(),
            },
        )
