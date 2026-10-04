"""Everything the agent remembers, in one SQLite file: chats, facts, jobs, usage and logins."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'chat',
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_chat ON messages(chat_id, id);
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL UNIQUE,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    cron TEXT NOT NULL,
    prompt TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    approved INTEGER NOT NULL DEFAULT 1,
    next_run REAL NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    job_name TEXT NOT NULL,
    started REAL NOT NULL,
    finished REAL,
    ok INTEGER,
    output TEXT
);
CREATE INDEX IF NOT EXISTS job_runs_started ON job_runs(started);
CREATE TABLE IF NOT EXISTS usage (
    day TEXT PRIMARY KEY,
    calls INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    created REAL NOT NULL,
    expires REAL NOT NULL,
    last_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class Job:
    id: int
    name: str
    cron: str
    prompt: str
    enabled: bool
    next_run: float
    approved: bool = True


class Memory:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).touch(mode=0o600, exist_ok=True)
            os.chmod(path, 0o600)  # only the agent's own user may open the database
        # timeout: wait up to 10 s instead of failing when another process (like `run-jobs`) is
        # writing at the same moment.
        self._db = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.execute("PRAGMA busy_timeout = 10000")
            if str(path) != ":memory:":
                self._db.execute("PRAGMA journal_mode = WAL")  # readers don't block the writer
            self._db.executescript(SCHEMA)
            self._migrate()
            self._db.commit()

    def _migrate(self) -> None:
        """Brings a database from an older version up to date (called under the lock)."""
        def columns(table: str) -> set[str]:
            return {r["name"] for r in self._db.execute(f"PRAGMA table_info({table})")}
        if "approved" not in columns("jobs"):
            # Jobs from before approvals existed need Roland's OK once, since some may have been
            # created by the agent itself.
            self._db.execute("ALTER TABLE jobs ADD COLUMN approved INTEGER NOT NULL DEFAULT 0")
        if "last_seen" not in columns("sessions"):
            # Old sessions can't be checked for idle time, so everyone logs in again.
            self._db.execute("DROP TABLE sessions")
            self._db.execute("CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, created REAL "
                             "NOT NULL, expires REAL NOT NULL, last_seen REAL NOT NULL)")

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur

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
        rows = self._all(
            "SELECT id, title, updated FROM chats WHERE kind = ? ORDER BY updated DESC", (kind,)
        )
        return [dict(r) for r in rows]

    def rename_chat(self, chat_id: int, title: str) -> None:
        self._exec("UPDATE chats SET title = ? WHERE id = ?", (title[:80], chat_id))

    def delete_chat(self, chat_id: int) -> bool:
        return self._exec("DELETE FROM chats WHERE id = ?", (chat_id,)).rowcount > 0

    def add_message(self, chat_id: int, role: str, content: str) -> None:
        now = time.time()
        self._exec(
            "INSERT INTO messages(chat_id, role, content, created) VALUES (?, ?, ?, ?)",
            (chat_id, role, content, now),
        )
        self._exec("UPDATE chats SET updated = ? WHERE id = ?", (now, chat_id))

    def messages(self, chat_id: int, limit: int | None = None) -> list[dict]:
        if limit is None:
            rows = self._all(
                "SELECT role, content, created FROM messages WHERE chat_id = ? ORDER BY id",
                (chat_id,),
            )
            return [dict(r) for r in rows]
        rows = self._all(
            "SELECT role, content, created FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        )
        return [dict(r) for r in reversed(rows)]

    # --- facts ---
    def remember(self, text: str) -> int:
        text = text.strip()
        self._exec("INSERT OR IGNORE INTO facts(text, created) VALUES (?, ?)", (text, time.time()))
        return int(self._all("SELECT id FROM facts WHERE text = ?", (text,))[0]["id"])

    def forget(self, fact_id: int) -> bool:
        return self._exec("DELETE FROM facts WHERE id = ?", (fact_id,)).rowcount > 0

    def facts(self) -> list[tuple[int, str]]:
        return [(r["id"], r["text"]) for r in self._all("SELECT id, text FROM facts ORDER BY id")]

    # --- jobs ---
    def add_job(self, name: str, cron: str, prompt: str, next_run: float,
                approved: bool = True) -> int:
        """Jobs the agent creates itself start unapproved (and off) until Roland approves them."""
        cur = self._exec(
            "INSERT INTO jobs(name, cron, prompt, enabled, approved, next_run, created) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name[:80], cron, prompt, int(approved), int(approved), next_run, time.time()),
        )
        return int(cur.lastrowid)

    def jobs(self) -> list[Job]:
        return [
            Job(r["id"], r["name"], r["cron"], r["prompt"], bool(r["enabled"]), r["next_run"],
                bool(r["approved"]))
            for r in self._all("SELECT * FROM jobs ORDER BY id")
        ]

    def job(self, job_id: int) -> Job | None:
        return next((j for j in self.jobs() if j.id == job_id), None)

    def due_jobs(self, now: float) -> list[Job]:
        return [j for j in self.jobs() if j.enabled and j.approved and j.next_run <= now]

    def set_next_run(self, job_id: int, next_run: float) -> None:
        self._exec("UPDATE jobs SET next_run = ? WHERE id = ?", (next_run, job_id))

    def set_job_enabled(self, job_id: int, enabled: bool) -> bool:
        return self._exec(
            "UPDATE jobs SET enabled = ? WHERE id = ?", (int(enabled), job_id)
        ).rowcount > 0

    def approve_job(self, job_id: int) -> bool:
        return self._exec(
            "UPDATE jobs SET approved = 1, enabled = 1 WHERE id = ?", (job_id,)
        ).rowcount > 0

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
            "INSERT INTO usage(day, calls) VALUES (?, 1) "
            "ON CONFLICT(day) DO UPDATE SET calls = calls + 1",
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
        self._exec("INSERT INTO meta(key, value) VALUES (?, ?) "
                   "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def delete_session(self, token_hash: str) -> None:
        self._exec("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def delete_expired_sessions(self, now: float) -> None:
        self._exec("DELETE FROM sessions WHERE expires <= ?", (now,))
