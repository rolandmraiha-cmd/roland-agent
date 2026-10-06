"""Local SQLite and workspace snapshots, retention, and an offline database restore."""

from __future__ import annotations

import gzip
import os
import re
import shutil
import sqlite3
import stat
import tarfile
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from .audit import Audit
from .config import Config
from .locking import database_lock, file_lock
from .memory import Memory
from .migrations import migrate
from .migrations.backup import database_snapshot

DB_NAME = re.compile(r"agent-([0-9]{8})-([0-9]{4})\.db\.gz\Z")
WORKSPACE_NAME = re.compile(r"workspace-([0-9]{8})\.tar\.gz\Z")
EXCLUDED = {".trash", ".uploads-tmp", ".sandbox-home/.cache"}


class WorkspaceTooLarge(ValueError):
    pass


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _tar_info(name: str, metadata: os.stat_result) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.mode = stat.S_IMODE(metadata.st_mode)
    info.mtime = int(metadata.st_mtime)
    info.uid, info.gid = metadata.st_uid, metadata.st_gid
    return info


def _archive_tree(
    archive: tarfile.TarFile, descriptor: int, prefix: str, total: list[int], limit: int
) -> None:
    for name in sorted(os.listdir(descriptor)):
        relative = f"{prefix}/{name}" if prefix else name
        if relative in EXCLUDED:
            continue
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode):
            info = _tar_info(relative, metadata)
            info.type = tarfile.SYMTYPE
            info.linkname = os.readlink(name, dir_fd=descriptor)
            archive.addfile(info)
        elif stat.S_ISDIR(metadata.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            try:
                info = _tar_info(relative, os.fstat(child))
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
                _archive_tree(archive, child, relative, total, limit)
            finally:
                os.close(child)
        elif stat.S_ISREG(metadata.st_mode):
            file_descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
            with os.fdopen(file_descriptor, "rb") as source:
                metadata = os.fstat(source.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise ValueError("Workspace backup refuses hard links or changed file types")
                total[0] += metadata.st_size
                if total[0] > limit:
                    raise WorkspaceTooLarge("Workspace exceeds BACKUP_WORKSPACE_MAX_MB")
                info = _tar_info(relative, metadata)
                info.size = metadata.st_size
                archive.addfile(info, source)
        # Sockets, devices and FIFOs are not portable workspace files; never open them.


def workspace_snapshot(workspace: Path, output: Path, max_bytes: int) -> Path:
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=output.parent, suffix=".tar.gz.tmp")
    try:
        with os.fdopen(descriptor, "wb") as packed:
            root = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                with tarfile.open(fileobj=packed, mode="w:gz", dereference=False) as archive:
                    _archive_tree(archive, root, "", [0], max_bytes)
            finally:
                os.close(root)
            packed.flush()
            os.fsync(packed.fileno())
        os.replace(temporary, output)
        _sync_directory(output.parent)
        return output
    finally:
        Path(temporary).unlink(missing_ok=True)


def _dated_files(folder: Path, pattern: re.Pattern) -> list[tuple[datetime, Path]]:
    result = []
    for path in folder.iterdir() if folder.exists() else ():
        match = pattern.fullmatch(path.name)
        if match and stat.S_ISREG(path.lstat().st_mode):
            try:
                stamp = "".join(match.groups())
                date = datetime.strptime(stamp, "%Y%m%d%H%M" if len(stamp) == 12 else "%Y%m%d").replace(
                    tzinfo=UTC
                )
            except ValueError:
                continue
            result.append((date, path))
    return sorted(result, reverse=True)


def prune_backups(config: Config) -> None:
    """Retain newest days and newest ISO weeks; ignore unrelated files and symlinks."""
    folder = config.backup_dir
    if folder is None:
        return
    daily, weekly = {}, {}
    snapshots = _dated_files(folder / "db", DB_NAME)
    for date, path in snapshots:
        daily.setdefault(date.date(), path)
        weekly.setdefault(date.isocalendar()[:2], path)
    keep = set(list(daily.values())[: config.backup_keep_daily])
    keep.update(list(weekly.values())[: config.backup_keep_weekly])
    for _, path in snapshots:
        if path not in keep:
            path.unlink()
    for _, path in _dated_files(folder / "workspace", WORKSPACE_NAME)[config.backup_workspace_keep :]:
        path.unlink()


def validate_backup_config(config: Config) -> None:
    if config.backup_dir is None:
        raise ValueError("Set BACKUP_DIR to enable backups")
    folder = config.backup_dir.resolve()
    workspace = config.workspace.resolve()
    if folder == workspace or workspace in folder.parents:
        raise ValueError("BACKUP_DIR must be outside the workspace")
    if config.backup_keep_daily < 1 or config.backup_keep_weekly < 0 or config.backup_workspace_keep < 1:
        raise ValueError("Backup retention must keep at least one daily and workspace snapshot")
    if config.backup_workspace_max_mb < 0:
        raise ValueError("BACKUP_WORKSPACE_MAX_MB cannot be negative")
    try:
        if not re.fullmatch(r"[0-9]{2}:[0-9]{2}", config.backup_time):
            raise ValueError("invalid time format")
        datetime.strptime(config.backup_time, "%H:%M")
    except ValueError as error:
        raise ValueError("BACKUP_TIME must be HH:MM") from error


def backup_now(config: Config, memory: Memory, audit: Audit, *, now: float | None = None) -> list[Path]:
    validate_backup_config(config)
    now = time.time() if now is None else now
    date = datetime.fromtimestamp(now, UTC)
    folder = config.backup_dir
    with file_lock(folder / ".backup.lock"):
        try:
            source = sqlite3.connect(config.db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
            try:
                db_file = folder / "db" / f"agent-{date:%Y%m%d-%H%M}.db.gz"
                outputs = [database_snapshot(source, db_file)]
            finally:
                source.close()
            workspace_status = "disabled"
            if config.backup_workspace:
                workspace_file = folder / "workspace" / f"workspace-{date:%Y%m%d}.tar.gz"
                try:
                    outputs.append(
                        workspace_snapshot(
                            config.workspace, workspace_file, config.backup_workspace_max_mb * 1024 * 1024
                        )
                    )
                    workspace_status = "saved"
                except WorkspaceTooLarge:
                    workspace_status = "skipped_over_limit"
            prune_backups(config)
            with memory.transaction():
                memory.set_meta("last_backup_ok", str(now))
                memory.set_meta("last_backup_error", "")
                audit.write(
                    "system",
                    "backup",
                    detail={
                        "ok": True,
                        "workspace": workspace_status,
                        "files": [path.name for path in outputs],
                    },
                )
            with memory._lock:
                memory._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return outputs
        except Exception as error:
            message = f"Backup failed: {type(error).__name__}"
            with memory.transaction():
                memory.set_meta("last_backup_error", message)
                audit.write("system", "backup", detail={"ok": False, "error": message})
            raise


def restore(config: Config, backup_file: Path, *, now: float | None = None) -> Path | None:
    """Prepare and migrate a verified copy before replacing an offline database."""
    now = time.time() if now is None else now
    with database_lock(config.data_dir, exclusive=True):
        descriptor, temporary = tempfile.mkstemp(dir=config.data_dir, suffix=".restore.db")
        moved = []
        try:
            with os.fdopen(descriptor, "wb") as target, gzip.open(backup_file, "rb") as source:
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
            with open(temporary, "rb") as source:
                if source.read(16) != b"SQLite format 3\x00":
                    raise ValueError("Restore requires a SQLite database snapshot")
            db = sqlite3.connect(temporary)
            try:
                if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("Restore failed integrity_check")
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if (
                    not {"chats", "messages", "facts", "jobs", "job_runs", "usage", "meta", "sessions"}
                    <= tables
                ):
                    raise ValueError("Restore requires an agent database")
                db.execute("PRAGMA foreign_keys=ON")
                migrate(db)
                if db.execute("PRAGMA foreign_key_check").fetchall():
                    raise ValueError("Restore failed foreign_key_check")
                db.execute("PRAGMA journal_mode=DELETE")
            finally:
                db.close()
            restored = Memory(temporary)
            try:
                audit = Audit.from_config(restored, config)
                if not audit.verify()["ok"]:
                    raise ValueError("Restore failed audit verification")
                with restored.transaction():
                    restored.delete_all_sessions()  # A restored old cookie must not log anyone in.
                    audit.write("roland", "backup", detail={"action": "restore", "file": backup_file.name})
                with restored._lock:
                    restored._db.execute("PRAGMA journal_mode=DELETE")
            finally:
                restored.close()
            stamp = datetime.fromtimestamp(now, UTC).strftime("%Y%m%d-%H%M%S-%f")
            preserved = config.db_path.with_name(f"agent.db.pre-restore-{stamp}")
            for suffix in ("", "-wal", "-shm", "-journal"):
                old = Path(str(config.db_path) + suffix)
                if old.exists():
                    saved = Path(str(preserved) + suffix)
                    if saved.exists():
                        raise FileExistsError("Pre-restore copy already exists")
                    os.rename(old, saved)
                    moved.append((old, saved))
            try:
                os.replace(temporary, config.db_path)
            except BaseException:
                for old, saved in reversed(moved):
                    os.rename(saved, old)
                moved.clear()
                raise
            _sync_directory(config.data_dir)
            return preserved if moved else None
        except BaseException:
            # A failure partway through preserving the old files leaves their original names.
            if Path(temporary).exists():
                for old, saved in reversed(moved):
                    os.rename(saved, old)
            raise
        finally:
            for suffix in ("", "-wal", "-shm", "-journal"):
                Path(temporary + suffix).unlink(missing_ok=True)
