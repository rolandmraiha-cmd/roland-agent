"""Files, upload/download/preview, and trash HTTP routes (§8.2 / M5.3)."""

from __future__ import annotations

import hashlib
import os
from pathlib import PurePosixPath
from typing import BinaryIO

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from ..workspace import (
    PREVIEW_MAX_BYTES,
    READ_CHUNK_BYTES,
    Workspace,
    WorkspaceError,
    content_disposition,
    image_mime,
    normalize,
)


def workspace_for(agent) -> Workspace:
    config = agent.config
    return Workspace(
        config.workspace,
        quota_mb=config.workspace_quota_mb,
        reserve_mb=config.workspace_reserve_mb,
        max_files=config.workspace_max_files,
        upload_max_mb=config.upload_max_mb,
        trash_keep_days=config.trash_keep_days,
        memory=agent.memory,
    )


class MkdirBody(BaseModel):
    path: str = Field(min_length=1, max_length=1024)


class MoveBody(BaseModel):
    from_: str = Field(alias="from", min_length=1, max_length=1024)
    to: str = Field(min_length=1, max_length=1024)

    model_config = {"populate_by_name": True}


def _usage_payload(ws: Workspace) -> dict:
    usage = ws.usage()
    return {
        "used_mb": round(usage.used_mb, 2),
        "quota_mb": usage.quota_mb,
        "free_mb": round(usage.free_mb, 2),
    }


def _http_for_workspace(error: Exception) -> HTTPException:
    if isinstance(error, FileNotFoundError):
        return HTTPException(404, "not found")
    if isinstance(error, FileExistsError):
        return HTTPException(409, "already exists")
    if isinstance(error, WorkspaceError):
        msg = str(error)
        if "workspace is full" in msg:
            return HTTPException(507, msg)
        return HTTPException(400, msg)
    return HTTPException(400, str(error))


def _download_chunks(handle: BinaryIO, size: int):
    try:
        remaining = size
        while remaining:
            chunk = handle.read(min(READ_CHUNK_BYTES, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        handle.close()


class _DownloadResponse(StreamingResponse):
    def __init__(self, handle: BinaryIO, size: int, **kwargs):
        self._handle = handle
        super().__init__(_download_chunks(handle, size), **kwargs)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Includes cancellation and ASGI 2.4 send failures, which can skip backgrounds.
            self._handle.close()


def build_router(agent) -> APIRouter:
    router = APIRouter()

    @router.get("/api/files")
    async def list_files(path: str = ""):
        ws = workspace_for(agent)
        try:
            normalize(path or "")
            entries, truncated = ws.list_dir(path or "")
        except (WorkspaceError, OSError) as error:
            raise _http_for_workspace(error) from error
        # Strip internal path key for API shape.
        public = [
            {
                "name": e["name"],
                "type": e["type"],
                "size": e["size"],
                "modified": e["modified"],
                "origin": e["origin"],
            }
            for e in entries
        ]
        return {
            "path": path or "",
            "entries": public,
            "truncated": truncated,
            "usage": _usage_payload(ws),
        }

    @router.put("/api/files/content")
    async def upload_content(request: Request, path: str):
        ws = workspace_for(agent)
        try:
            components = normalize(path)
            if not components:
                raise WorkspaceError("give a file name.")
        except WorkspaceError as error:
            raise _http_for_workspace(error) from error

        overwrite = request.headers.get("x-overwrite", "0").strip() == "1"
        max_bytes = ws.upload_max_bytes()
        # Refuse early on Content-Length when present, but always enforce while streaming.
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) > max_bytes:
                    raise HTTPException(413, "upload too large")
            except ValueError:
                pass

        try:
            ws.refuse_symlink_final(path)
        except WorkspaceError as error:
            raise _http_for_workspace(error) from error
        if not overwrite and ws.exists(path):
            raise HTTPException(409, "already exists")

        tmp_rel = None
        digest = hashlib.sha256()
        size = 0
        try:
            try:
                ws.check_space(0)
            except WorkspaceError as error:
                raise _http_for_workspace(error) from error
            tmp_rel, handle, _fd = ws.open_upload_tmp()
            try:
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > max_bytes:
                        raise HTTPException(413, "upload too large")
                    try:
                        ws.check_space(len(chunk))
                    except WorkspaceError as error:
                        raise _http_for_workspace(error) from error
                    handle.write(chunk)
                    digest.update(chunk)
                handle.flush()
            finally:
                handle.close()

            sha = digest.hexdigest()
            result = ws.finalize_upload(
                tmp_rel,
                path,
                overwrite=overwrite,
                origin="upload",
                sha256=sha,
                size=size,
                deleted_by="roland",
            )
            tmp_rel = None
            agent.audit.write(
                "roland",
                "file_upload",
                detail={"path": result["path"], "size": size, "sha256": sha},
            )
            return JSONResponse(result, status_code=201)
        except HTTPException:
            if tmp_rel:
                ws.discard_upload_tmp(tmp_rel)
            raise
        except (WorkspaceError, FileExistsError, OSError) as error:
            if tmp_rel:
                ws.discard_upload_tmp(tmp_rel)
            raise _http_for_workspace(error) from error

    @router.get("/api/files/download")
    async def download(path: str):
        ws = workspace_for(agent)
        handle = None
        try:
            handle = ws.open_read(path)
            size = os.fstat(handle.fileno()).st_size
            name = PurePosixPath(path).name or "download"
        except (WorkspaceError, FileNotFoundError, OSError) as error:
            if handle is not None:
                handle.close()
            raise _http_for_workspace(error) from error
        return _DownloadResponse(
            handle,
            size,
            media_type="application/octet-stream",
            headers={
                "Content-Length": str(size),
                "Content-Disposition": content_disposition(name),
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox",
                "Cache-Control": "no-store",
            },
        )

    @router.get("/api/files/preview")
    async def preview(path: str):
        ws = workspace_for(agent)
        try:
            data = ws.read_bytes(path, max_bytes=PREVIEW_MAX_BYTES + 1)
        except (WorkspaceError, FileNotFoundError, OSError) as error:
            raise _http_for_workspace(error) from error
        if len(data) > PREVIEW_MAX_BYTES:
            raise HTTPException(415, "too large for preview")
        mime = image_mime(data[:16] if len(data) >= 16 else data)
        if mime is None:
            raise HTTPException(415, "not a previewable image")
        return Response(
            content=data,
            media_type=mime,
            headers={
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox",
                "Cache-Control": "no-store",
            },
        )

    @router.post("/api/files/mkdir")
    async def mkdir(body: MkdirBody):
        ws = workspace_for(agent)
        try:
            created = ws.mkdir(body.path)
        except (WorkspaceError, OSError) as error:
            raise _http_for_workspace(error) from error
        return JSONResponse({"path": created}, status_code=201)

    @router.post("/api/files/move")
    async def move(body: MoveBody):
        ws = workspace_for(agent)
        try:
            if ws.exists(body.to):
                raise FileExistsError(body.to)
            dest = ws.move(body.from_, body.to, overwrite=False)
        except (WorkspaceError, FileExistsError, FileNotFoundError, OSError) as error:
            raise _http_for_workspace(error) from error
        agent.audit.write("roland", "file_move", detail={"from": body.from_, "to": dest})
        return {"path": dest}

    @router.delete("/api/files")
    async def delete(path: str):
        ws = workspace_for(agent)
        try:
            trash_id = ws.move_to_trash(path, deleted_by="roland")
        except (WorkspaceError, FileNotFoundError, OSError) as error:
            raise _http_for_workspace(error) from error
        agent.audit.write("roland", "file_delete", detail={"path": path, "trash_id": trash_id})
        return {"ok": True, "trash_id": trash_id}

    @router.get("/api/trash")
    async def list_trash():
        rows = agent.memory.trash_entries()
        return [
            {
                "id": r["id"],
                "original_path": r["original_path"],
                "trash_path": r["trash_path"],
                "deleted_by": r["deleted_by"],
                "size": r["size"],
                "deleted": r["deleted"],
            }
            for r in rows
        ]

    @router.post("/api/trash/{trash_id}/restore")
    async def restore(trash_id: str):
        ws = workspace_for(agent)
        try:
            path = ws.restore_trash(trash_id)
        except FileExistsError as error:
            raise HTTPException(409, "original path exists") from error
        except FileNotFoundError as error:
            raise HTTPException(404, "not found") from error
        except (WorkspaceError, OSError) as error:
            raise _http_for_workspace(error) from error
        agent.audit.write("roland", "file_restore", detail={"id": trash_id, "path": path})
        return {"path": path}

    return router
