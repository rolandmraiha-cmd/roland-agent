"""Everything the agent remembers, in one SQLite file: chats, facts, jobs, usage and logins."""

from __future__ import annotations

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
    expires REAL NOT NULL
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


class Memory:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.executescript(SCHEMA)
            self._db.commit()

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
    def add_job(self, name: str, cron: str, prompt: str, next_run: float) -> int:
        cur = self._exec(
            "INSERT INTO jobs(name, cron, prompt, next_run, created) VALUES (?, ?, ?, ?, ?)",
            (name[:80], cron, prompt, next_run, time.time()),
        )
        return int(cur.lastrowid)

    def jobs(self) -> list[Job]:
        return [
            Job(r["id"], r["name"], r["cron"], r["prompt"], bool(r["enabled"]), r["next_run"])
            for r in self._all("SELECT * FROM jobs ORDER BY id")
        ]

    def job(self, job_id: int) -> Job | None:
        return next((j for j in self.jobs() if j.id == job_id), None)

    def due_jobs(self, now: float) -> list[Job]:
        return [j for j in self.jobs() if j.enabled and j.next_run <= now]

    def set_next_run(self, job_id: int, next_run: float) -> None:
        self._exec("UPDATE jobs SET next_run = ? WHERE id = ?", (next_run, job_id))

    def set_job_enabled(self, job_id: int, enabled: bool) -> bool:
        return self._exec(
            "UPDATE jobs SET enabled = ? WHERE id = ?", (int(enabled), job_id)
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

    # --- login sessions (only a hash of each token is stored) ---
    def add_session(self, token_hash: str, expires: float) -> None:
        self._exec(
            "INSERT INTO sessions(token_hash, created, expires) VALUES (?, ?, ?)",
            (token_hash, time.time(), expires),
        )

    def session_valid(self, token_hash: str, now: float) -> bool:
        return bool(
            self._all(
                "SELECT 1 FROM sessions WHERE token_hash = ? AND expires > ?", (token_hash, now)
            )
        )

    def delete_session(self, token_hash: str) -> None:
        self._exec("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def delete_expired_sessions(self, now: float) -> None:
        self._exec("DELETE FROM sessions WHERE expires <= ?", (now,))
