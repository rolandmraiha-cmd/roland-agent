"""Extra file tools: delete, move, info, attach (§9.3 / M5.2)."""

from __future__ import annotations

import mimetypes
from pathlib import PurePosixPath

from .workspace import Workspace, WorkspaceError, image_mime


def workspace_from_ctx(ctx) -> Workspace:
    """Build a Workspace bound to the tool context's root and config."""
    config = getattr(ctx, "config", None)
    return Workspace(
        ctx.workspace,
        quota_mb=getattr(config, "workspace_quota_mb", 8192) if config else 8192,
        reserve_mb=getattr(config, "workspace_reserve_mb", 256) if config else 256,
        max_files=getattr(config, "workspace_max_files", 50_000) if config else 50_000,
        upload_max_mb=getattr(config, "upload_max_mb", 100) if config else 100,
        trash_keep_days=getattr(config, "trash_keep_days", 7) if config else 7,
        memory=ctx.memory,
    )


async def delete_file(ctx, args: dict) -> str:
    path = str(args.get("path", ""))
    try:
        ws = workspace_from_ctx(ctx)
        run = ctx.run
        approval_id = getattr(run, "pending_approval_id", None) if run is not None else None
        ws.move_to_trash(path, deleted_by="agent", approval_id=approval_id)
        return f"Moved {path} to trash."
    except FileNotFoundError:
        return f"Error: file not found: {path or '(empty path)'}."
    except (WorkspaceError, ValueError, OSError) as error:
        return f"Error: {error}"


async def move_file(ctx, args: dict) -> str:
    src = str(args.get("from", args.get("src", "")))
    dst = str(args.get("to", args.get("dst", "")))
    try:
        ws = workspace_from_ctx(ctx)
        run = ctx.run
        # §9.3 fail-closed: overwrite only when replace was already gate-approved.
        approved_replace = run is not None and getattr(run, "pending_approval_id", None) is not None
        ws.move(src, dst, overwrite=approved_replace)
        return f"Moved {src} → {dst}."
    except FileExistsError:
        return f"Error: destination already exists: {dst}"
    except FileNotFoundError:
        return f"Error: file not found: {src or '(empty path)'}."
    except (WorkspaceError, ValueError, OSError) as error:
        return f"Error: {error}"


async def file_info(ctx, args: dict) -> str:
    path = str(args.get("path", ""))
    try:
        info = workspace_from_ctx(ctx).info(path)
        return (
            f"path: {info['path']}\n"
            f"type: {info['type']}\n"
            f"size: {info['size']}\n"
            f"modified: {info['modified']}\n"
            f"sha256: {info['sha256'] or '(none)'}\n"
            f"origin: {info['origin']}"
        )
    except FileNotFoundError:
        return f"Error: file not found: {path or '(empty path)'}."
    except (WorkspaceError, ValueError, OSError) as error:
        return f"Error: {error}"


async def attach_file(ctx, args: dict) -> str:
    path = str(args.get("path", ""))
    note = str(args.get("note", ""))[:200]
    try:
        ws = workspace_from_ctx(ctx)
        info = ws.info(path)
        if info["type"] != "file":
            return "Error: attach_file only works on regular files."
        name = PurePosixPath(info["path"]).name
        header = b""
        try:
            header = ws.read_bytes(path, max_bytes=16)
        except OSError:
            header = b""
        mime = image_mime(header) or mimetypes.guess_type(name)[0] or "application/octet-stream"
        preview = image_mime(header) is not None and info["size"] <= 10 * 1024 * 1024
        run = ctx.run
        if run is not None:
            await run.events.put(
                {
                    "type": "file",
                    "path": info["path"],
                    "name": name,
                    "size": info["size"],
                    "mime": mime,
                    "preview": preview,
                    "note": note,
                }
            )
            # Persist a file timeline row when we have a chat.
            if run.chat_id is not None:
                try:
                    ctx.memory.add_event(
                        run.chat_id,
                        "file",
                        f"Shared {info['path']} with Roland.",
                        meta={
                            "path": info["path"],
                            "name": name,
                            "size": info["size"],
                            "mime": mime,
                            "preview": preview,
                        },
                        run_id=run.run_id,
                    )
                except (TypeError, ValueError, OSError):
                    # Timeline write is best-effort; the SSE card already went out.
                    pass
        return f"Shared {info['path']} with Roland."
    except FileNotFoundError:
        return f"Error: file not found: {path or '(empty path)'}."
    except (WorkspaceError, ValueError, OSError) as error:
        return f"Error: {error}"
