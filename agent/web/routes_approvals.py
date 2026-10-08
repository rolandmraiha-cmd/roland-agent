"""Approvals and audit HTTP routes (§8.2)."""

from __future__ import annotations

import csv
import io
import json
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field


class ApproveBody(BaseModel):
    args_hash: str = Field(min_length=64, max_length=64)
    confirm: bool = False


class RejectBody(BaseModel):
    note: str | None = Field(default=None, max_length=500)
    args_hash: str | None = Field(default=None, max_length=64)
    alternative: dict[str, Any] | None = None


def build_router(agent) -> APIRouter:
    router = APIRouter()

    @router.get("/api/approvals")
    async def list_approvals(
        status: str = "pending",
        chat_id: int | None = None,
        limit: int = 100,
        before: float | None = None,
    ):
        if status not in {"pending", "all", "approved", "rejected", "expired", "cancelled", "executed", "failed"}:
            raise HTTPException(400, "invalid status")
        rows = agent.memory.approvals(status=status, chat_id=chat_id, limit=limit, before=before)
        from ..gate import approval_public

        return [approval_public(row) for row in rows]

    @router.get("/api/approvals/{approval_id}")
    async def get_approval(approval_id: str):
        row = agent.memory.approval(approval_id)
        if row is None:
            raise HTTPException(404, "no such approval")
        from ..gate import approval_public

        return approval_public(row)

    @router.post("/api/approvals/{approval_id}/approve")
    async def approve(approval_id: str, body: ApproveBody):
        try:
            result = await agent.gate.approve(approval_id, body.args_hash, confirm=body.confirm)
            return result
        except KeyError:
            raise HTTPException(404, "no such approval") from None
        except PermissionError:
            raise HTTPException(400, "confirm must be true for this category") from None
        except LookupError as error:
            raise HTTPException(409, str(error) or "not pending") from None

    @router.post("/api/approvals/{approval_id}/reject")
    async def reject(approval_id: str, body: RejectBody):
        from ..training.capture import validate_action

        try:
            alternative = validate_action(body.alternative) if body.alternative is not None else None
            result = await agent.gate.reject(approval_id, note=body.note, args_hash=body.args_hash, alternative=alternative)
            return result
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        except KeyError:
            raise HTTPException(404, "no such approval") from None
        except LookupError as error:
            raise HTTPException(409, str(error) or "not pending") from None

    @router.get("/api/audit")
    async def list_audit(
        event: str | None = None,
        tool: str | None = None,
        decision: str | None = None,
        since: float | None = None,
        until: float | None = None,
        limit: int = 100,
        before: int | None = None,
    ):
        return agent.memory.audit_rows(
            event=event, tool=tool, decision=decision, since=since, until=until,
            limit=limit, before=before,
        )

    @router.get("/api/audit/export.csv")
    async def export_audit(
        event: str | None = None,
        tool: str | None = None,
        decision: str | None = None,
        since: float | None = None,
        until: float | None = None,
        limit: int = 5000,
        before: int | None = None,
    ):
        rows = agent.memory.audit_rows(
            event=event, tool=tool, decision=decision, since=since, until=until,
            limit=limit, before=before,
        )
        buf = io.StringIO()
        writer = csv.DictWriter(
            buf,
            fieldnames=["id", "ts", "actor", "event", "run_id", "chat_id", "tool", "decision", "detail"],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({
                **{k: row.get(k) for k in ("id", "ts", "actor", "event", "run_id", "chat_id", "tool", "decision")},
                "detail": json.dumps(row.get("detail"), ensure_ascii=False),
            })
        return PlainTextResponse(
            buf.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="audit.csv"'},
        )

    @router.get("/api/audit/verify")
    async def verify_audit():
        return agent.audit.verify()

    return router
