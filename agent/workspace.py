"""Race-free workspace path normalisation and file operations (§6.4)."""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote

# Soft defaults match Config / §11.1.
DEFAULT_QUOTA_MB = 8192
DEFAULT_RESERVE_MB = 256
DEFAULT_MAX_FILES = 50_000
DEFAULT_UPLOAD_MAX_MB = 100
WRITE_FILE_MAX_BYTES = 1_000_000
PREVIEW_MAX_BYTES = 10 * 1024 * 1024
LIST_CAP = 1000
PATH_MAX_CHARS = 1024
COMPONENT_MAX_BYTES = 255

SPECIAL_DIRS = (
    ".trash",
    ".uploads-tmp",
    ".sandbox-home",
    "browser",
    "browser/downloads",
    "browser/uploads",
    "screenshots",
    "uploads",
)

_SYMLINK_MSG = "Path is outside the workspace (symbolic links aren't followed)."
_OUTSIDE_MSG = "Path is outside the workspace."


class WorkspaceError(ValueError):
    """Invalid or unsafe workspace path. Messages must keep the v1 "outside" substring."""


def normalize(rel: str) -> tuple[str, ...]:
    """Return path components under the workspace root, or raise WorkspaceError."""
    if not isinstance(rel, str):
        raise WorkspaceError(_OUTSIDE_MSG)
    if len(rel) > PATH_MAX_CHARS:
        raise WorkspaceError("Path is too long.")
    if "\x00" in rel:
        raise WorkspaceError(_OUTSIDE_MSG)
    for ch in rel:
        o = ord(ch)
        if o < 0x20 or o == 0x7F:
            raise WorkspaceError(_OUTSIDE_MSG)
    if "\\" in rel:
        raise WorkspaceError(_OUTSIDE_MSG)
    if rel.startswith("~"):
        raise WorkspaceError(_OUTSIDE_MSG)
    if rel.startswith("/"):
        raise WorkspaceError(_OUTSIDE_MSG)

    parts: list[str] = []
    for raw in rel.split("/"):
        if raw in ("", "."):
            continue
        if raw == "..":
            raise WorkspaceError(_OUTSIDE_MSG)
        if len(raw.encode("utf-8")) > COMPONENT_MAX_BYTES:
            raise WorkspaceError("Path component is too long.")
        parts.append(raw)
    return tuple(parts)


def join_rel(components: tuple[str, ...]) -> str:
    return "/".join(components)


def image_mime(header: bytes) -> str | None:
    """Return an image MIME type when *header* starts with a real image magic, else None."""
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"GIF87a") or header.startswith(b"GIF89a"):
        return "image/gif"
    if len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp"
    return None


def content_disposition(filename: str) -> str:
    """RFC 5987 attachment disposition; ASCII fallback strips quotes and non-latin1."""
    safe = filename.replace("\\", "_").replace('"', "_")
    ascii_name = "".join(c if 32 <= ord(c) < 127 and c not in {";", "\\"} else "_" for c in safe) or "download"
    encoded = quote(filename, safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}"


@dataclass
class QuotaStatus:
    used_bytes: int
    free_bytes: int
    total_bytes: int
    quota_mb: int
    reserve_mb: int

    @property
    def used_mb(self) -> float:
        return self.used_bytes / (1024 * 1024)

    @property
    def free_mb(self) -> float:
        return self.free_bytes / (1024 * 1024)


class Workspace:
    """openat/O_NOFOLLOW workspace rooted at *root*."""

    def __init__(
        self,
        root: Path | str,
        *,
        quota_mb: int = DEFAULT_QUOTA_MB,
        reserve_mb: int = DEFAULT_RESERVE_MB,
        max_files: int = DEFAULT_MAX_FILES,
        upload_max_mb: int = DEFAULT_UPLOAD_MAX_MB,
        write_file_max: int = WRITE_FILE_MAX_BYTES,
        trash_keep_days: int = 7,
        memory=None,
    ):
        self.root = Path(root)
        self.quota_mb = quota_mb
        self.reserve_mb = reserve_mb
        self.max_files = max_files
        self.upload_max_mb = upload_max_mb
        self.write_file_max = write_file_max
        self.trash_keep_days = trash_keep_days
        self.memory = memory
        self._file_count_cache: tuple[float, int] | None = None

    # --- setup ---
    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for rel in SPECIAL_DIRS:
            (self.root / rel).mkdir(parents=True, exist_ok=True)

    # --- quota ---
    def statvfs(self) -> os.statvfs_result:
        return os.statvfs(self.root)

    def _walk_size(self) -> int:
        total = 0
        root = str(self.root)
        for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
            for name in filenames:
                try:
                    total += os.lstat(os.path.join(dirpath, name)).st_size
                except OSError:
                    continue
        return total

    def usage(self) -> QuotaStatus:
        st = self.statvfs()
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        fs_used = max(0, total - free)
        quota_bytes = self.quota_mb * 1024 * 1024
        # Dedicated workspace FS is ~quota-sized; shared host disks (tests) use a walk.
        used = fs_used if total <= max(quota_bytes * 2, 20 * 1024 * 1024 * 1024) else self._walk_size()
        return QuotaStatus(used, free, total, self.quota_mb, self.reserve_mb)

    def check_space(self, n: int) -> None:
        """Raise WorkspaceError('workspace is full') when soft quota or reserve would break."""
        if n < 0:
            raise WorkspaceError("Negative size.")
        st = self.usage()
        if st.free_bytes - n < self.reserve_mb * 1024 * 1024:
            raise WorkspaceError("workspace is full")
        if st.used_bytes + n > self.quota_mb * 1024 * 1024:
            raise WorkspaceError("workspace is full")

    def file_count(self, *, force: bool = False) -> int:
        now = time.monotonic()
        if not force and self._file_count_cache and now - self._file_count_cache[0] < 60:
            return self._file_count_cache[1]
        count = 0
        for _dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
            # Do not descend into .trash for the soft count? Spec says cached walk; count all.
            count += len(filenames) + len(dirnames)
        self._file_count_cache = (now, count)
        return count

    def check_file_count(self, extra: int = 1) -> None:
        if self.file_count() + extra > self.max_files:
            raise WorkspaceError("workspace is full")

    # --- openat walk ---
    def _open_root(self) -> int:
        self.ensure()
        return os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)

    def _map_symlink_err(self, error: OSError) -> WorkspaceError:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            return WorkspaceError(_SYMLINK_MSG)
        if isinstance(error, FileNotFoundError) or error.errno == errno.ENOENT:
            return error  # type: ignore[return-value]
        return WorkspaceError(str(error))

    def _walk_dirs(self, root_fd: int, components: tuple[str, ...]) -> int:
        """Open the directory containing the final component; returns dir_fd (caller closes)."""
        fd = root_fd
        owned = False
        try:
            for comp in components[:-1] if components else ():
                try:
                    nxt = os.open(
                        comp,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=fd,
                    )
                except OSError as error:
                    mapped = self._map_symlink_err(error)
                    if mapped is error:
                        raise
                    raise mapped from error
                if owned:
                    os.close(fd)
                fd = nxt
                owned = True
            if owned:
                # Caller owns fd; don't close on success.
                out = fd
                owned = False
                return out
            # Duplicate root so caller can always close.
            return os.dup(root_fd)
        finally:
            if owned:
                os.close(fd)

    def _open_at(
        self,
        components: tuple[str, ...],
        flags: int,
        *,
        mode: int = 0o644,
        create_parents: bool = False,
    ) -> tuple[int, int]:
        """Return (dir_fd, file_fd). Caller closes both. Empty components → open root as dir."""
        if not components:
            root = self._open_root()
            return root, os.dup(root)
        root = self._open_root()
        try:
            if create_parents:
                self._mkdir_parents(root, components[:-1])
            dir_fd = self._walk_dirs(root, components)
        except BaseException:
            os.close(root)
            raise
        os.close(root)
        name = components[-1]
        try:
            file_fd = os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=dir_fd)
        except OSError as error:
            os.close(dir_fd)
            mapped = self._map_symlink_err(error)
            if mapped is error:
                raise
            raise mapped from error
        return dir_fd, file_fd

    def _mkdir_parents(self, root_fd: int, components: tuple[str, ...]) -> None:
        fd = root_fd
        owned = False
        try:
            for comp in components:
                try:
                    os.mkdir(comp, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
                except OSError as error:
                    mapped = self._map_symlink_err(error)
                    if mapped is error:
                        raise
                    raise mapped from error
                try:
                    nxt = os.open(
                        comp,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=fd,
                    )
                except OSError as error:
                    mapped = self._map_symlink_err(error)
                    if mapped is error:
                        raise
                    raise mapped from error
                if owned:
                    os.close(fd)
                fd = nxt
                owned = True
        finally:
            if owned:
                os.close(fd)

    def exists(self, rel: str) -> bool:
        components = normalize(rel)
        if not components:
            return True
        try:
            dir_fd, file_fd = self._open_at(components, os.O_RDONLY)
        except (FileNotFoundError, WorkspaceError):
            return False
        except OSError as error:
            if error.errno == errno.ENOENT:
                return False
            raise
        else:
            os.close(file_fd)
            os.close(dir_fd)
            return True

    def lstat(self, rel: str) -> os.stat_result:
        components = normalize(rel)
        root = self._open_root()
        try:
            if not components:
                return os.fstat(root)
            dir_fd = self._walk_dirs(root, components)
            try:
                return os.lstat(components[-1], dir_fd=dir_fd)
            finally:
                os.close(dir_fd)
        except OSError as error:
            mapped = self._map_symlink_err(error)
            if mapped is error:
                raise
            raise mapped from error
        finally:
            os.close(root)

    # --- read / write / list ---
    def read_bytes(self, rel: str, *, max_bytes: int | None = None) -> bytes:
        components = normalize(rel)
        if not components:
            raise WorkspaceError("give a file name.")
        dir_fd, file_fd = self._open_at(components, os.O_RDONLY)
        try:
            meta = os.fstat(file_fd)
            if stat.S_ISLNK(meta.st_mode):
                raise WorkspaceError(_SYMLINK_MSG)
            if not stat.S_ISREG(meta.st_mode):
                raise WorkspaceError("Not a regular file.")
            with os.fdopen(file_fd, "rb") as handle:
                file_fd = -1  # ownership transferred
                if max_bytes is None:
                    return handle.read()
                return handle.read(max_bytes)
        finally:
            if file_fd >= 0:
                os.close(file_fd)
            os.close(dir_fd)

    def read_text(self, rel: str, *, max_chars: int) -> str:
        # Read a bit more than max_chars in bytes; decode with replace.
        data = self.read_bytes(rel, max_bytes=max_chars * 4 + 16)
        text = data.decode("utf-8", errors="replace")
        if len(text) > max_chars:
            return text[:max_chars] + "\n... [cut, file continues]"
        return text

    def write_bytes(
        self,
        rel: str,
        data: bytes,
        *,
        overwrite: bool = False,
        append: bool = False,
        origin: str = "agent",
        chat_id: int | None = None,
        deleted_by: str = "agent",
        approval_id: str | None = None,
        enforce_write_cap: bool = False,
    ) -> dict:
        components = normalize(rel)
        if not components:
            raise WorkspaceError("give a file name.")
        if components[0] == ".trash":
            raise WorkspaceError("Cannot write into .trash.")
        if enforce_write_cap and len(data) > self.write_file_max:
            raise WorkspaceError(f"Content is larger than {self.write_file_max} bytes.")
        self.check_space(len(data))
        existed = False
        try:
            meta = self.lstat(rel)
            if stat.S_ISLNK(meta.st_mode):
                raise WorkspaceError(_SYMLINK_MSG)
            if stat.S_ISDIR(meta.st_mode):
                raise WorkspaceError("Not a regular file.")
            existed = stat.S_ISREG(meta.st_mode)
        except FileNotFoundError:
            existed = False

        if existed and not overwrite and not append:
            raise FileExistsError(rel)
        if existed and (overwrite or not append):
            # Overwrite: move old to trash first (§9.3 / A5.1).
            self.move_to_trash(rel, deleted_by=deleted_by, approval_id=approval_id)
            existed = False

        if append and self.exists(rel):
            self.check_space(len(data))
            dir_fd, file_fd = self._open_at(components, os.O_WRONLY | os.O_APPEND)
            try:
                with os.fdopen(file_fd, "ab") as handle:
                    file_fd = -1
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                if file_fd >= 0:
                    os.close(file_fd)
                os.close(dir_fd)
        else:
            self.check_file_count(1)
            self.check_space(len(data))
            # Write via temp in same directory then replace.
            parent = components[:-1]
            name = components[-1]
            root = self._open_root()
            try:
                if parent:
                    self._mkdir_parents(root, parent)
                dir_fd = self._walk_dirs(root, components) if components else os.dup(root)
            except BaseException:
                os.close(root)
                raise
            os.close(root)
            tmp_name = f".__ws_tmp_{uuid.uuid4().hex}"
            try:
                tmp_fd = os.open(
                    tmp_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o644,
                    dir_fd=dir_fd,
                )
                try:
                    with os.fdopen(tmp_fd, "wb") as handle:
                        tmp_fd = -1
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(tmp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
                finally:
                    if tmp_fd >= 0:
                        os.close(tmp_fd)
                    try:
                        os.unlink(tmp_name, dir_fd=dir_fd)
                    except FileNotFoundError:
                        pass
            except OSError as error:
                mapped = self._map_symlink_err(error)
                if mapped is error:
                    raise
                raise mapped from error
            finally:
                os.close(dir_fd)

        digest = hashlib.sha256(data).hexdigest() if not append else None
        size = self.lstat(rel).st_size
        path = join_rel(components)
        if self.memory is not None:
            if digest is None:
                # Append: recompute from file when small enough, else leave prior.
                try:
                    digest = hashlib.sha256(self.read_bytes(rel)).hexdigest()
                except OSError:
                    digest = None
            self.memory.record_file(path, size, digest, origin=origin, chat_id=chat_id)
        self._file_count_cache = None
        return {"path": path, "size": size, "sha256": digest}

    def write_text(
        self,
        rel: str,
        content: str,
        *,
        append: bool = False,
        origin: str = "agent",
        chat_id: int | None = None,
        deleted_by: str = "agent",
        approval_id: str | None = None,
    ) -> dict:
        data = content.encode("utf-8")
        return self.write_bytes(
            rel,
            data,
            overwrite=not append,
            append=append,
            origin=origin,
            chat_id=chat_id,
            deleted_by=deleted_by,
            approval_id=approval_id,
            enforce_write_cap=True,
        )

    def list_dir(self, rel: str = "") -> tuple[list[dict], bool]:
        components = normalize(rel or "")
        root = self._open_root()
        owned_extra = False
        dir_fd = root
        try:
            if components:
                # Walk all components as directories.
                fd = root
                for _i, comp in enumerate(components):
                    try:
                        nxt = os.open(
                            comp,
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=fd,
                        )
                    except OSError as error:
                        mapped = self._map_symlink_err(error)
                        if mapped is error:
                            raise
                        raise mapped from error
                    if owned_extra:
                        os.close(fd)
                    elif fd != root:
                        os.close(fd)
                    fd = nxt
                    owned_extra = True
                dir_fd = fd
            names = sorted(os.listdir(dir_fd))
            truncated = len(names) > LIST_CAP
            entries = []
            for name in names[:LIST_CAP]:
                try:
                    meta = os.lstat(name, dir_fd=dir_fd)
                except OSError:
                    continue
                if stat.S_ISLNK(meta.st_mode):
                    kind = "symlink"
                elif stat.S_ISDIR(meta.st_mode):
                    kind = "dir"
                else:
                    kind = "file"
                rel_path = join_rel(components + (name,)) if components else name
                origin = "unknown"
                if self.memory is not None and kind == "file":
                    try:
                        row = self.memory.file(rel_path)
                        if row:
                            origin = row.get("origin") or "unknown"
                    except ValueError:
                        pass
                entries.append(
                    {
                        "name": name + ("@" if kind == "symlink" else ""),
                        "type": kind,
                        "size": meta.st_size if kind == "file" else 0,
                        "modified": meta.st_mtime,
                        "origin": origin,
                        "path": rel_path,
                    }
                )
            return entries, truncated
        finally:
            if owned_extra and dir_fd != root:
                os.close(dir_fd)
            os.close(root)

    def mkdir(self, rel: str) -> str:
        components = normalize(rel)
        if not components:
            raise WorkspaceError("give a folder name.")
        if components[0] == ".trash":
            raise WorkspaceError("Cannot write into .trash.")
        self.check_file_count(1)
        root = self._open_root()
        try:
            self._mkdir_parents(root, components)
        finally:
            os.close(root)
        self._file_count_cache = None
        return join_rel(components)

    # --- trash ---
    def move_to_trash(
        self,
        rel: str,
        *,
        deleted_by: str = "agent",
        approval_id: str | None = None,
    ) -> str:
        components = normalize(rel)
        if not components:
            raise WorkspaceError("give a file name.")
        if components[0] == ".trash":
            raise WorkspaceError("Already in trash.")
        meta = self.lstat(rel)
        size = meta.st_size if stat.S_ISREG(meta.st_mode) else 0
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        trash_id = uuid.uuid4().hex
        bucket = f".trash/{stamp}-{trash_id}"
        # Recreate original relative path under the bucket.
        dest_components = normalize(f"{bucket}/{join_rel(components)}")
        root = self._open_root()
        try:
            self._mkdir_parents(root, dest_components[:-1])
            src_dir = self._walk_dirs(root, components)
            try:
                dst_dir = self._walk_dirs(root, dest_components)
                try:
                    os.rename(
                        components[-1],
                        dest_components[-1],
                        src_dir_fd=src_dir,
                        dst_dir_fd=dst_dir,
                    )
                finally:
                    os.close(dst_dir)
            finally:
                os.close(src_dir)
        except OSError as error:
            mapped = self._map_symlink_err(error)
            if mapped is error:
                raise
            raise mapped from error
        finally:
            os.close(root)

        trash_path = join_rel(dest_components)
        original = join_rel(components)
        record_id = trash_id
        if self.memory is not None:
            record_id = self.memory.add_trash(
                original, trash_path, deleted_by, size, approval_id=approval_id
            )
            self.memory.delete_file_record(original)
        self._file_count_cache = None
        return record_id

    def purge_trash(self, *, older_than_days: int | None = None, now: float | None = None) -> int:
        days = self.trash_keep_days if older_than_days is None else older_than_days
        now = time.time() if now is None else now
        cutoff = now - days * 86400
        removed = 0
        if self.memory is not None:
            for row in self.memory.trash_entries(deleted_before=cutoff):
                rel = row["trash_path"]
                try:
                    self._rm_tree(rel)
                except (FileNotFoundError, WorkspaceError, OSError):
                    pass
                self.memory.delete_trash_record(row["id"])
                removed += 1
        # Also sweep orphaned .trash buckets by mtime when no DB row.
        trash_root = self.root / ".trash"
        if trash_root.is_dir():
            for entry in list(trash_root.iterdir()):
                try:
                    if entry.is_symlink():
                        continue
                    if entry.stat().st_mtime < cutoff:
                        if entry.is_dir():
                            shutil.rmtree(entry, ignore_errors=True)
                        else:
                            entry.unlink(missing_ok=True)
                        removed += 1
                except OSError:
                    continue
        self._file_count_cache = None
        return removed

    def _rm_tree(self, rel: str) -> None:
        components = normalize(rel)
        path = self.root.joinpath(*components)
        # Only delete under .trash.
        if not components or components[0] != ".trash":
            raise WorkspaceError(_OUTSIDE_MSG)
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path, ignore_errors=True)

    def restore_trash(self, trash_id: str) -> str:
        if self.memory is None:
            raise WorkspaceError("No trash index.")
        row = self.memory.trash_entry(trash_id)
        if row is None:
            raise FileNotFoundError(trash_id)
        original = row["original_path"]
        trash_path = row["trash_path"]
        if self.exists(original):
            raise FileExistsError(original)
        src = normalize(trash_path)
        dst = normalize(original)
        root = self._open_root()
        try:
            self._mkdir_parents(root, dst[:-1])
            src_dir = self._walk_dirs(root, src)
            try:
                dst_dir = self._walk_dirs(root, dst)
                try:
                    os.rename(src[-1], dst[-1], src_dir_fd=src_dir, dst_dir_fd=dst_dir)
                finally:
                    os.close(dst_dir)
            finally:
                os.close(src_dir)
        finally:
            os.close(root)
        self.memory.delete_trash_record(trash_id)
        self._file_count_cache = None
        return original

    def move(self, src: str, dst: str, *, overwrite: bool = False) -> str:
        src_c = normalize(src)
        dst_c = normalize(dst)
        if not src_c or not dst_c:
            raise WorkspaceError("give a file name.")
        if src_c[0] == ".trash" or dst_c[0] == ".trash":
            raise WorkspaceError("Cannot move into or out of .trash with move().")
        if self.exists(dst):
            if not overwrite:
                raise FileExistsError(dst)
            self.move_to_trash(dst, deleted_by="agent")
        root = self._open_root()
        try:
            self._mkdir_parents(root, dst_c[:-1])
            src_dir = self._walk_dirs(root, src_c)
            try:
                dst_dir = self._walk_dirs(root, dst_c)
                try:
                    os.rename(src_c[-1], dst_c[-1], src_dir_fd=src_dir, dst_dir_fd=dst_dir)
                finally:
                    os.close(dst_dir)
            finally:
                os.close(src_dir)
        except OSError as error:
            mapped = self._map_symlink_err(error)
            if mapped is error:
                raise
            raise mapped from error
        finally:
            os.close(root)
        src_path, dst_path = join_rel(src_c), join_rel(dst_c)
        if self.memory is not None:
            row = None
            try:
                row = self.memory.file(src_path)
            except ValueError:
                row = None
            self.memory.delete_file_record(src_path)
            size = self.lstat(dst_path).st_size
            origin = row.get("origin", "unknown") if row else "unknown"
            chat_id = row.get("chat_id") if row else None
            sha = row.get("sha256") if row else None
            self.memory.record_file(dst_path, size, sha, origin=origin, chat_id=chat_id)
        self._file_count_cache = None
        return dst_path

    def info(self, rel: str) -> dict:
        components = normalize(rel)
        if not components:
            raise WorkspaceError("give a file name.")
        meta = self.lstat(rel)
        kind = (
            "symlink"
            if stat.S_ISLNK(meta.st_mode)
            else "dir"
            if stat.S_ISDIR(meta.st_mode)
            else "file"
        )
        path = join_rel(components)
        origin = "unknown"
        sha = None
        if self.memory is not None:
            try:
                row = self.memory.file(path)
                if row:
                    origin = row.get("origin") or "unknown"
                    sha = row.get("sha256")
            except ValueError:
                pass
        if kind == "file" and sha is None:
            try:
                sha = hashlib.sha256(self.read_bytes(rel)).hexdigest()
            except (WorkspaceError, OSError):
                sha = None
        return {
            "path": path,
            "type": kind,
            "size": meta.st_size,
            "modified": meta.st_mtime,
            "sha256": sha,
            "origin": origin,
        }

    # --- upload helpers ---
    def upload_max_bytes(self) -> int:
        return self.upload_max_mb * 1024 * 1024

    def open_upload_tmp(self) -> tuple[str, BinaryIO, int]:
        """Create an exclusive temp file under .uploads-tmp/; returns (rel, fileobj, fd)."""
        self.ensure()
        name = f".uploads-tmp/{uuid.uuid4().hex}.part"
        components = normalize(name)
        dir_fd, file_fd = self._open_at(
            components, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode=0o600, create_parents=True
        )
        os.close(dir_fd)
        return join_rel(components), os.fdopen(file_fd, "wb"), file_fd

    def finalize_upload(
        self,
        tmp_rel: str,
        dest_rel: str,
        *,
        overwrite: bool = False,
        origin: str = "upload",
        chat_id: int | None = None,
        sha256: str | None = None,
        size: int = 0,
        deleted_by: str = "roland",
    ) -> dict:
        if overwrite and self.exists(dest_rel):
            self.move_to_trash(dest_rel, deleted_by=deleted_by)
        elif self.exists(dest_rel):
            raise FileExistsError(dest_rel)
        self.move(tmp_rel, dest_rel, overwrite=False)
        # move() tried to update agent origin; re-record as upload.
        if self.memory is not None:
            self.memory.record_file(dest_rel, size, sha256, origin=origin, chat_id=chat_id)
        return {"path": dest_rel, "size": size, "sha256": sha256}

    def discard_upload_tmp(self, tmp_rel: str) -> None:
        try:
            components = normalize(tmp_rel)
            if not components or components[0] != ".uploads-tmp":
                return
            root = self._open_root()
            try:
                dir_fd = self._walk_dirs(root, components)
                try:
                    os.unlink(components[-1], dir_fd=dir_fd)
                finally:
                    os.close(dir_fd)
            finally:
                os.close(root)
        except (FileNotFoundError, WorkspaceError, OSError):
            pass
