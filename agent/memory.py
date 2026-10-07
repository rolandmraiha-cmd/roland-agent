"""Everything the agent remembers, in one SQLite file: chats, facts, jobs, usage and logins."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from uuid import uuid4

from .migrations import current_version, latest_version, migrate
from .migrations.backup import pre_migration_backup
from .migrations.m0001_v1_baseline import SCHEMA as SCHEMA  # Preserve the v1 schema import.

EVENT_KINDS = {"tool", "approval", "signin", "file", "error", "note"}
APPROVAL_CATEGORIES = {
    "payment",
    "message",
    "public_post",
    "delete",
    "form_submit",
    "upload",
    "shell",
    "memory",
    "job",
    "other",
}


def canonical_json(value: dict, max_bytes: int) -> str:
    if not isinstance(value, dict):
        raise ValueError("Metadata and action arguments must be JSON objects")
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    if len(encoded.encode("utf-8")) > max_bytes:
        raise ValueError(f"JSON exceeds the {max_bytes}-byte storage limit")
    return encoded


def relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if not path.parts or path.is_absolute() or ".." in path.parts or "\\" in value or "\x00" in value:
        raise ValueError("Metadata paths must be workspace-relative without traversal")
    return path.as_posix()


@dataclass
class Job:
    id: int
    name: str
    cron: str
    prompt: str
    enabled: bool
    next_run: float
    approved: bool = True
    origin: str = "panel"  # 'panel' (Roland), 'agent' (schedule_job) or 'old' (before v1 merge)


class Memory:
    def __init__(self, path: Path | str, *, backup_dir: Path | None = None):
        existing_database = str(path) != ":memory:" and Path(path).is_file() and Path(path).stat().st_size > 0
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).touch(mode=0o600, exist_ok=True)
            os.chmod(path, 0o600)  # only the agent's own user may open the database
        # timeout: wait up to 10 s instead of failing when another process (like `run-jobs`) is
        # writing at the same moment.
        self._db = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._transaction_depth = 0
        try:
            with self._lock:
                self._db.execute("PRAGMA foreign_keys = ON")
                self._db.execute("PRAGMA busy_timeout = 10000")
                if str(path) != ":memory:":
                    self._db.execute("PRAGMA journal_mode = WAL")
                self._db.execute("PRAGMA synchronous = NORMAL")
                version = current_version(self._db)
                if (
                    (version >= 1 or existing_database)
                    and version < latest_version()
                    and backup_dir is not None
                    and backup_dir.is_dir()
                ):
                    pre_migration_backup(self._db, backup_dir, version, latest_version())
                migrate(self._db)
        except BaseException:
            self._db.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @property
    def schema_version(self) -> int:
        with self._lock:
            return current_version(self._db)

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self.transaction():
            cur = self._db.execute(sql, args)
            return cur

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Commit related changes together, including nested Memory and audit writes."""
        with self._lock:
            depth = self._transaction_depth
            savepoint = f"memory_transaction_{depth}"
            self._db.execute("BEGIN IMMEDIATE" if depth == 0 else f"SAVEPOINT {savepoint}")
            self._transaction_depth += 1
            try:
                yield self._db
                if depth == 0:
                    self._db.commit()
                else:
                    self._db.execute(f"RELEASE SAVEPOINT {savepoint}")
            except BaseException:
                if depth == 0:
                    self._db.rollback()
                else:
                    self._db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    self._db.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            finally:
                self._transaction_depth -= 1

    def _all(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # --- chats ---
    def new_chat(self, title: str = "New chat", kind: str = "chat") -> int:
        now = time.time()
        cur = self._exec(
            "INSERT INTO chats(title, kind, created, updated) VALUES (?, ?, ?, ?)",
            (title[:80], kind, now, now),
        )
        return int(cur.lastrowid)

    def chat_exists(self, chat_id: int) -> bool:
        return bool(self._all("SELECT 1 FROM chats WHERE id = ?", (chat_id,)))

    def chats(self, kind: str = "chat") -> list[dict]:
        rows = self._all("SELECT id, title, updated FROM chats WHERE kind = ? ORDER BY updated DESC", (kind,))
        return [dict(r) for r in rows]

    def rename_chat(self, chat_id: int, title: str) -> None:
        self._exec("UPDATE chats SET title = ? WHERE id = ?", (title[:80], chat_id))

    def delete_chat(self, chat_id: int) -> bool:
        return self._exec("DELETE FROM chats WHERE id = ?", (chat_id,)).rowcount > 0

    def add_message(self, chat_id: int, role: str, content: str) -> None:
        self._add_message(chat_id, role, content, "text", None, None)

    def _add_message(
        self, chat_id: int, role: str, content: str, kind: str, meta: str | None, run_id: str | None
    ) -> int:
        now = time.time()
        with self.transaction():
            cursor = self._db.execute(
                "INSERT INTO messages(chat_id, role, content, created, kind, meta, run_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (chat_id, role, content, now, kind, meta, run_id),
            )
            self._db.execute("UPDATE chats SET updated = ? WHERE id = ?", (now, chat_id))
            return int(cursor.lastrowid)

    def add_event(
        self, chat_id: int, kind: str, content: str, meta: dict | None = None, run_id: str | None = None
    ) -> int:
        if kind not in EVENT_KINDS:
            raise ValueError("Unknown timeline event kind")
        encoded = canonical_json(meta, 16 * 1024) if meta is not None else None
        return self._add_message(chat_id, "system", content, kind, encoded, run_id)

    def timeline(self, chat_id: int) -> list[dict]:
        rows = self._all("SELECT * FROM messages WHERE chat_id = ? ORDER BY id", (chat_id,))
        return [dict(row, meta=json.loads(row["meta"]) if row["meta"] is not None else None) for row in rows]

    def messages(self, chat_id: int, limit: int | None = None) -> list[dict]:
        if limit is None:
            rows = self._all(
                "SELECT role, content, created FROM messages WHERE chat_id = ? AND kind = 'text' ORDER BY id",
                (chat_id,),
            )
            return [dict(r) for r in rows]
        rows = self._all(
            "SELECT role, content, created FROM messages WHERE chat_id = ? AND kind = 'text' ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        )
        return [dict(r) for r in reversed(rows)]

    # --- facts ---
    def remember(
        self,
        text: str,
        limit: int | None = None,
        *,
        origin: str = "unknown",
        chat_id: int | None = None,
        tainted: bool = False,
    ) -> int | None:
        """Saves a fact and returns its id (the old id if it's saved already). With a limit, a new
        fact is only added while fewer than `limit` are saved, checked in the same statement so
        two processes can't both add the last one. Returns None when it didn't fit."""
        text = text.strip()
        if origin not in {"agent", "panel", "unknown"}:
            raise ValueError("Unknown fact origin")
        self._exec(
            "INSERT OR IGNORE INTO facts(text, created, origin, chat_id, tainted) SELECT ?, ?, ?, ?, ? "
            "WHERE (SELECT COUNT(*) FROM facts) < ?",
            (text, time.time(), origin, chat_id, int(tainted), limit if limit is not None else 2**62),
        )
        rows = self._all("SELECT id FROM facts WHERE text = ?", (text,))
        return int(rows[0]["id"]) if rows else None

    def forget(self, fact_id: int) -> bool:
        return self._exec("DELETE FROM facts WHERE id = ?", (fact_id,)).rowcount > 0

    def facts(self) -> list[tuple[int, str]]:
        return [(r["id"], r["text"]) for r in self._all("SELECT id, text FROM facts ORDER BY id")]

    def facts_detailed(self) -> list[dict]:
        return [
            dict(row, tainted=bool(row["tainted"])) for row in self._all("SELECT * FROM facts ORDER BY id")
        ]

    # --- jobs ---
    def add_job(
        self, name: str, cron: str, prompt: str, next_run: float, approved: bool = True, origin: str = "panel"
    ) -> int:
        """Jobs the agent creates itself start unapproved (and off) until Roland approves them."""
        cur = self._exec(
            "INSERT INTO jobs(name, cron, prompt, enabled, approved, origin, next_run, created) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (name[:80], cron, prompt, int(approved), int(approved), origin, next_run, time.time()),
        )
        return int(cur.lastrowid)

    def jobs(self) -> list[Job]:
        return [
            Job(
                r["id"],
                r["name"],
                r["cron"],
                r["prompt"],
                bool(r["enabled"]),
                r["next_run"],
                bool(r["approved"]),
                r["origin"],
            )
            for r in self._all("SELECT * FROM jobs ORDER BY id")
        ]

    def job(self, job_id: int) -> Job | None:
        return next((j for j in self.jobs() if j.id == job_id), None)

    def due_jobs(self, now: float) -> list[Job]:
        return [j for j in self.jobs() if j.enabled and j.approved and j.next_run <= now]

    def set_next_run(self, job_id: int, next_run: float) -> None:
        self._exec("UPDATE jobs SET next_run = ? WHERE id = ?", (next_run, job_id))

    def set_job_enabled(self, job_id: int, enabled: bool) -> bool:
        return self._exec("UPDATE jobs SET enabled = ? WHERE id = ?", (int(enabled), job_id)).rowcount > 0

    def approve_job(self, job_id: int) -> bool:
        return self._exec("UPDATE jobs SET approved = 1, enabled = 1 WHERE id = ?", (job_id,)).rowcount > 0

    def delete_job(self, job_id: int) -> bool:
        return self._exec("DELETE FROM jobs WHERE id = ?", (job_id,)).rowcount > 0

    def start_run(self, job: Job) -> int:
        cur = self._exec(
            "INSERT INTO job_runs(job_id, job_name, started) VALUES (?, ?, ?)",
            (job.id, job.name, time.time()),
        )
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, ok: bool, output: str) -> None:
        self._exec(
            "UPDATE job_runs SET finished = ?, ok = ?, output = ? WHERE id = ?",
            (time.time(), int(ok), output, run_id),
        )

    def runs(self, limit: int = 30) -> list[dict]:
        rows = self._all("SELECT * FROM job_runs ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # --- usage ---
    def calls_today(self, day: str) -> int:
        rows = self._all("SELECT calls FROM usage WHERE day = ?", (day,))
        return int(rows[0]["calls"]) if rows else 0

    def count_call(self, day: str) -> int:
        self._exec(
            "INSERT INTO usage(day, calls) VALUES (?, 1) ON CONFLICT(day) DO UPDATE SET calls = calls + 1",
            (day,),
        )
        return self.calls_today(day)

    def take_call(self, day: str, limit: int) -> bool:
        """Counts one model call if today's total is still under the limit, in one statement, so
        two processes (the server and `run-jobs`) can't both slip past the cap."""
        if limit <= 0:
            return False
        cur = self._exec(
            "INSERT INTO usage(day, calls) VALUES (?, 1) "
            "ON CONFLICT(day) DO UPDATE SET calls = calls + 1 WHERE calls < ?",
            (day, limit),
        )
        return cur.rowcount > 0

    def fail_unfinished_runs(self) -> int:
        """Marks runs that never finished (the agent stopped mid-job) as failed."""
        return self._exec(
            "UPDATE job_runs SET finished = ?, ok = 0, "
            "output = COALESCE(output, '') || 'Stopped: the agent restarted before this job finished.' "
            "WHERE finished IS NULL",
            (time.time(),),
        ).rowcount

    # --- login sessions (only a hash of each token is stored) ---
    def add_session(self, token_hash: str, expires: float) -> None:
        now = time.time()
        self._exec(
            "INSERT INTO sessions(token_hash, created, expires, last_seen) VALUES (?, ?, ?, ?)",
            (token_hash, now, expires, now),
        )

    def session_valid(self, token_hash: str, now: float, idle: float) -> bool:
        """True if the session exists, hasn't expired and was used within `idle` seconds."""
        rows = self._all(
            "SELECT last_seen FROM sessions WHERE token_hash = ? AND expires > ? AND last_seen > ?",
            (token_hash, now, now - idle),
        )
        if not rows:
            return False
        if now - rows[0]["last_seen"] > 60:
            self._exec("UPDATE sessions SET last_seen = ? WHERE token_hash = ?", (now, token_hash))
        return True

    def delete_all_sessions(self) -> None:
        self._exec("DELETE FROM sessions")

    def get_meta(self, key: str) -> str | None:
        rows = self._all("SELECT value FROM meta WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        self._exec(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def delete_session(self, token_hash: str) -> None:
        self._exec("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def delete_expired_sessions(self, now: float) -> None:
        self._exec("DELETE FROM sessions WHERE expires <= ?", (now,))

    # --- v2 run and approval records (execution and web routes arrive later) ---
    def add_agent_run(
        self,
        origin: str,
        *,
        run_id: str | None = None,
        chat_id: int | None = None,
        job_id: int | None = None,
        job_run_id: int | None = None,
    ) -> str:
        identifier = run_id or uuid4().hex
        self._exec(
            "INSERT INTO runs(id, origin, chat_id, job_id, job_run_id, started) VALUES (?, ?, ?, ?, ?, ?)",
            (identifier, origin, chat_id, job_id, job_run_id, time.time()),
        )
        return identifier

    def agent_run(self, run_id: str) -> dict | None:
        rows = self._all("SELECT * FROM runs WHERE id = ?", (run_id,))
        return dict(rows[0], tainted=bool(rows[0]["tainted"])) if rows else None

    def set_run_tainted(self, run_id: str) -> bool:
        return (
            self._exec("UPDATE runs SET tainted = 1 WHERE id = ? AND status = 'running'", (run_id,)).rowcount
            > 0
        )

    def finish_agent_run(self, run_id: str, status: str = "done") -> bool:
        if status not in {"done", "error", "stopped"}:
            raise ValueError("A finished run must be done, error or stopped")
        return (
            self._exec(
                "UPDATE runs SET status = ?, finished = ? WHERE id = ? AND status = 'running'",
                (status, time.time(), run_id),
            ).rowcount
            > 0
        )

    def add_approval(
        self,
        run_id: str,
        tool: str,
        args: dict,
        category: str,
        summary: str,
        details: dict,
        expires: float,
        *,
        chat_id: int | None = None,
        job_id: int | None = None,
        model_reason: str | None = None,
        tainted: bool = False,
        needs_confirm: bool = False,
        screenshot_path: str | None = None,
    ) -> str:
        if category not in APPROVAL_CATEGORIES:
            raise ValueError("Unknown approval category")
        encoded = canonical_json(args, 64 * 1024)
        detail = canonical_json(details, 16 * 1024)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        identifier = secrets.token_urlsafe(16)
        needs_confirm = needs_confirm or category in {"payment", "message", "public_post", "delete"}
        screenshot = relative_path(screenshot_path) if screenshot_path is not None else None
        self._exec(
            "INSERT INTO approvals(id, run_id, chat_id, job_id, tool, args_json, args_hash, category, "
            "summary, details, model_reason, tainted, needs_confirm, screenshot_path, status, created, expires) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (
                identifier,
                run_id,
                chat_id,
                job_id,
                tool,
                encoded,
                digest,
                category,
                summary,
                detail,
                model_reason[:300] if model_reason is not None else None,
                int(tainted),
                int(needs_confirm),
                screenshot,
                time.time(),
                expires,
            ),
        )
        return identifier

    @staticmethod
    def _approval_record(row: sqlite3.Row) -> dict:
        return dict(
            row,
            details=json.loads(row["details"]),
            tainted=bool(row["tainted"]),
            needs_confirm=bool(row["needs_confirm"]),
        )

    def approval(self, approval_id: str) -> dict | None:
        rows = self._all("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        return self._approval_record(rows[0]) if rows else None

    def approvals(self, status: str = "pending", *, chat_id: int | None = None,
                   limit: int | None = None, before: float | None = None) -> list[dict]:
        if status == "all":
            sql = ("SELECT * FROM approvals WHERE (? IS NULL OR chat_id = ?) "
                   "AND (? IS NULL OR created < ?) ORDER BY created DESC, id DESC")
            params: list = [chat_id, chat_id, before, before]
        else:
            sql = ("SELECT * FROM approvals WHERE status = ? AND (? IS NULL OR chat_id = ?) "
                   "AND (? IS NULL OR created < ?) ORDER BY created DESC, id DESC")
            params = [status, chat_id, chat_id, before, before]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self._all(sql, tuple(params))
        return [self._approval_record(row) for row in rows]

    def count_pending_approvals(self) -> int:
        rows = self._all("SELECT COUNT(*) AS n FROM approvals WHERE status = 'pending'")
        return int(rows[0]["n"])

    def approvals_for_run(self, run_id: str, status: str = "pending") -> list[dict]:
        rows = self._all(
            "SELECT * FROM approvals WHERE run_id = ? AND status = ? ORDER BY created, id",
            (run_id, status),
        )
        return [self._approval_record(row) for row in rows]

    def expire_all_pending_approvals(self, now: float) -> int:
        return self._exec(
            "UPDATE approvals SET status = 'expired', decided = ? WHERE status = 'pending'",
            (now,),
        ).rowcount

    def expire_approvals_returning_ids(self, now: float) -> list[str]:
        rows = self._all(
            "SELECT id FROM approvals WHERE status = 'pending' AND expires <= ?",
            (now,),
        )
        ids = [row["id"] for row in rows]
        if ids:
            self.expire_approvals(now)
        return ids

    def decide_approval(
        self,
        approval_id: str,
        decision: str,
        args_hash: str,
        *,
        confirm: bool = False,
        note: str | None = None,
        now: float | None = None,
    ) -> bool:
        """One atomic decision, bound to the reviewed action and an unexpired pending record."""
        if decision not in {"approved", "rejected"}:
            raise ValueError("An approval decision must be approved or rejected")
        timestamp = time.time() if now is None else now
        return (
            self._exec(
                "UPDATE approvals SET status = ?, decided = ?, decision_note = ? "
                "WHERE id = ? AND status = 'pending' AND args_hash = ? AND expires > ? "
                "AND (? = 'rejected' OR needs_confirm = 0 OR ? = 1)",
                (
                    decision,
                    timestamp,
                    note[:500] if note is not None else None,
                    approval_id,
                    args_hash,
                    timestamp,
                    decision,
                    int(confirm),
                ),
            ).rowcount
            > 0
        )

    def finish_approval(self, approval_id: str, ok: bool, result: str) -> bool:
        digest = hashlib.sha256(result.encode("utf-8")).hexdigest()
        return (
            self._exec(
                "UPDATE approvals SET status = ?, executed = ?, result_digest = ? "
                "WHERE id = ? AND status = 'approved'",
                ("executed" if ok else "failed", time.time(), digest, approval_id),
            ).rowcount
            > 0
        )

    def cancel_approval(self, approval_id: str) -> bool:
        return (
            self._exec(
                "UPDATE approvals SET status = 'cancelled', decided = ? WHERE id = ? AND status = 'pending'",
                (time.time(), approval_id),
            ).rowcount
            > 0
        )

    def expire_approvals(self, now: float) -> int:
        return self._exec(
            "UPDATE approvals SET status = 'expired', decided = ? WHERE status = 'pending' AND expires <= ?",
            (now, now),
        ).rowcount

    def delete_approval(self, approval_id: str) -> bool:
        # Pending or approved actions cannot disappear while a caller is waiting on them.
        return (
            self._exec(
                "DELETE FROM approvals WHERE id = ? AND status IN ('rejected','expired','cancelled','executed','failed')",
                (approval_id,),
            ).rowcount
            > 0
        )


    def audit_rows(
        self,
        *,
        event: str | None = None,
        tool: str | None = None,
        decision: str | None = None,
        since: float | None = None,
        until: float | None = None,
        limit: int = 100,
        before: int | None = None,
    ) -> list[dict]:
        sql = (
            "SELECT id, ts, actor, event, run_id, chat_id, tool, decision, detail FROM audit_log WHERE 1=1"
        )
        params: list = []
        if event:
            sql += " AND event = ?"
            params.append(event)
        if tool:
            sql += " AND tool = ?"
            params.append(tool)
        if decision:
            sql += " AND decision = ?"
            params.append(decision)
        if since is not None:
            sql += " AND ts >= ?"
            params.append(since)
        if until is not None:
            sql += " AND ts <= ?"
            params.append(until)
        if before is not None:
            sql += " AND id < ?"
            params.append(before)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        rows = self._all(sql, tuple(params))
        return [
            dict(row, detail=json.loads(row["detail"]) if isinstance(row["detail"], str) else row["detail"])
            for row in rows
        ]

    # --- workspace metadata only; these methods never perform filesystem actions ---
    def record_file(
        self,
        path: str,
        size: int,
        sha256: str | None = None,
        *,
        origin: str = "unknown",
        chat_id: int | None = None,
    ) -> None:
        if size < 0:
            raise ValueError("File size cannot be negative")
        normalized = relative_path(path)
        now = time.time()
        self._exec(
            "INSERT INTO files(path, size, sha256, origin, chat_id, created, updated) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET size = excluded.size, sha256 = excluded.sha256, "
            "origin = excluded.origin, chat_id = excluded.chat_id, updated = excluded.updated",
            (normalized, size, sha256, origin, chat_id, now, now),
        )

    def file(self, path: str) -> dict | None:
        rows = self._all("SELECT * FROM files WHERE path = ?", (relative_path(path),))
        return dict(rows[0]) if rows else None

    def files(self) -> list[dict]:
        return [dict(row) for row in self._all("SELECT * FROM files ORDER BY path")]

    def delete_file_record(self, path: str) -> bool:
        return self._exec("DELETE FROM files WHERE path = ?", (relative_path(path),)).rowcount > 0

    def add_trash(
        self,
        original_path: str,
        trash_path: str,
        deleted_by: str,
        size: int,
        *,
        approval_id: str | None = None,
    ) -> str:
        original, target = relative_path(original_path), relative_path(trash_path)
        if not target.startswith(".trash/") or deleted_by not in {"roland", "agent"} or size < 0:
            raise ValueError("Trash metadata must use .trash/, a known actor and a nonnegative size")
        identifier = uuid4().hex
        self._exec(
            "INSERT INTO trash(id, original_path, trash_path, deleted_by, approval_id, size, deleted) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (identifier, original, target, deleted_by, approval_id, size, time.time()),
        )
        return identifier

    def trash_entry(self, trash_id: str) -> dict | None:
        rows = self._all("SELECT * FROM trash WHERE id = ?", (trash_id,))
        return dict(rows[0]) if rows else None

    def trash_entries(self, *, deleted_before: float | None = None) -> list[dict]:
        return [
            dict(row)
            for row in self._all(
                "SELECT * FROM trash WHERE (? IS NULL OR deleted < ?) ORDER BY deleted, id",
                (deleted_before, deleted_before),
            )
        ]

    def delete_trash_record(self, trash_id: str) -> bool:
        return self._exec("DELETE FROM trash WHERE id = ?", (trash_id,)).rowcount > 0

    # --- sign-in and screen records; no credentials or browser actions are accepted ---
    def add_signin_request(
        self, run_id: str, url: str, expires: float, *, chat_id: int | None = None, reason: str | None = None
    ) -> str:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or len(url) > 2048
            or len(parsed.hostname) > 253
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("Sign-in URL must be HTTP(S) without credentials and within the size limits")
        identifier = secrets.token_urlsafe(16)
        self._exec(
            "INSERT INTO signin_requests(id, run_id, chat_id, site, url, reason, status, created, expires) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (
                identifier,
                run_id,
                chat_id,
                parsed.hostname,
                url,
                reason[:300] if reason is not None else None,
                time.time(),
                expires,
            ),
        )
        return identifier

    def signin_request(self, request_id: str) -> dict | None:
        rows = self._all("SELECT * FROM signin_requests WHERE id = ?", (request_id,))
        return dict(rows[0]) if rows else None

    def signin_requests(self, status: str = "pending") -> list[dict]:
        return [
            dict(row)
            for row in self._all(
                "SELECT * FROM signin_requests WHERE status = ? ORDER BY created, id",
                (status,),
            )
        ]

    def set_signin_status(self, request_id: str, status: str, *, expected_status: str = "pending") -> bool:
        allowed = {
            "pending": {"in_progress", "cancelled", "expired"},
            "in_progress": {"done", "cancelled", "expired"},
        }
        if status not in allowed.get(expected_status, set()):
            raise ValueError("Invalid sign-in status transition")
        return (
            self._exec(
                "UPDATE signin_requests SET status = ?, finished = ? WHERE id = ? AND status = ?",
                (status, None if status == "in_progress" else time.time(), request_id, expected_status),
            ).rowcount
            > 0
        )

    def expire_signin_requests(self, now: float) -> int:
        return self._exec(
            "UPDATE signin_requests SET status = 'expired', finished = ? "
            "WHERE status IN ('pending','in_progress') AND expires <= ?",
            (now, now),
        ).rowcount

    def add_screen_session(
        self, session_hash: str, mode: str = "watch", *, signin_id: str | None = None
    ) -> str:
        if len(session_hash) != 64 or any(char not in "0123456789abcdef" for char in session_hash):
            raise ValueError("Screen sessions accept a SHA256 login-session hash, never a raw token")
        identifier = secrets.token_urlsafe(16)
        now = time.time()
        self._exec(
            "INSERT INTO screen_sessions(id, session_hash, mode, signin_id, started, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (identifier, session_hash, mode, signin_id, now, now),
        )
        return identifier

    def screen_session(self, screen_id: str) -> dict | None:
        rows = self._all("SELECT * FROM screen_sessions WHERE id = ?", (screen_id,))
        return dict(rows[0]) if rows else None

    def screen_sessions(self, *, active_only: bool = True) -> list[dict]:
        return [
            dict(row)
            for row in self._all(
                "SELECT * FROM screen_sessions WHERE ? = 0 OR ended IS NULL ORDER BY started, id",
                (int(active_only),),
            )
        ]

    def touch_screen_session(self, screen_id: str, session_hash: str) -> bool:
        return (
            self._exec(
                "UPDATE screen_sessions SET last_seen = ? WHERE id = ? AND session_hash = ? AND ended IS NULL",
                (time.time(), screen_id, session_hash),
            ).rowcount
            > 0
        )

    def end_screen_session(self, screen_id: str, session_hash: str) -> bool:
        return (
            self._exec(
                "UPDATE screen_sessions SET ended = ? WHERE id = ? AND session_hash = ? AND ended IS NULL",
                (time.time(), screen_id, session_hash),
            ).rowcount
            > 0
        )

    def expire_screen_sessions(self, idle_before: float) -> int:
        return self._exec(
            "UPDATE screen_sessions SET ended = ? WHERE ended IS NULL AND last_seen <= ?",
            (time.time(), idle_before),
        ).rowcount
