"""Workspace-relative path checks for sandboxd cwd (§6.3 / §6.4 essentials)."""

from __future__ import annotations

from pathlib import Path


class WorkspaceError(ValueError):
    """Path is not allowed inside the workspace."""


def resolve_cwd(workspace: Path, cwd: str) -> Path:
    """Resolve cwd under workspace. Raises WorkspaceError with 'outside the workspace' when bad."""
    if not isinstance(cwd, str):
        raise WorkspaceError("Path is outside the workspace.")
    if len(cwd) > 1024 or "\x00" in cwd or "\\" in cwd:
        raise WorkspaceError("Path is outside the workspace.")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in cwd):
        raise WorkspaceError("Path is outside the workspace.")
    if cwd.startswith("~") or cwd.startswith("/"):
        raise WorkspaceError("Path is outside the workspace.")
    root = workspace.resolve()
    for part in Path(cwd).parts:
        if part == "..":
            raise WorkspaceError("Path is outside the workspace.")
        if len(part.encode("utf-8")) > 255:
            raise WorkspaceError("Path is outside the workspace.")
    target = (root / (cwd or ".")).resolve()
    if target != root and root not in target.parents:
        raise WorkspaceError("Path is outside the workspace.")
    if not target.is_dir():
        raise WorkspaceError(f"cwd is not a directory: {cwd}")
    return target
