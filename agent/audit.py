"""Private, append-only action records with secret redaction and a SHA-256 chain."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import fields
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config
    from .memory import Memory

FIRST_HASH = "0" * 64
HASH_FIELDS = ("ts", "actor", "event", "run_id", "chat_id", "tool", "decision", "detail")


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _digest(previous: str, row: dict) -> str:
    document = {name: row[name] for name in HASH_FIELDS}
    document["detail"] = json.loads(document["detail"])
    return hashlib.sha256((previous + _json(document)).encode("utf-8")).hexdigest()


def _verify(db: sqlite3.Connection) -> dict:
    previous = FIRST_HASH
    count = 0
    for values in db.execute("SELECT * FROM audit_log ORDER BY id"):
        row = dict(zip(("id", *HASH_FIELDS, "prev_hash", "hash"), values, strict=True))
        count += 1
        try:
            detail = json.loads(row["detail"])
            valid = (
                isinstance(detail, dict)
                and math.isfinite(row["ts"])
                and row["prev_hash"] == previous
                and hmac.compare_digest(row["hash"], _digest(previous, row))
            )
        except (ValueError, TypeError, UnicodeError, RecursionError):
            valid = False
        if not valid:
            return {"ok": False, "rows": count, "first_bad_id": row["id"]}
        previous = row["hash"]
    return {"ok": True, "rows": count, "first_bad_id": None}


def verify_file(path: Path) -> dict:
    """Inspect an existing database without creating or upgrading it."""
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return _verify(db)
    finally:
        db.close()


class Audit:
    def __init__(self, memory: Memory, secrets: Iterable[str] = (), max_bytes: int = 8192):
        if max_bytes < 64:
            raise ValueError("AUDIT_DETAIL_MAX_BYTES must be at least 64")
        self.memory = memory
        self.max_bytes = max_bytes
        values = sorted({value for value in secrets if value}, key=len, reverse=True)
        # Escaped literals only. Replace in one pass so the marker isn't redacted again.
        self._secrets = re.compile("|".join(re.escape(value) for value in values)) if values else None

    @classmethod
    def from_config(cls, memory: Memory, config: Config) -> Audit:
        values = [getattr(config, field.name) for field in fields(config) if field.metadata.get("secret")]
        return cls(memory, values, config.audit_detail_max_bytes)

    def _redact(self, value: object) -> object:
        if isinstance(value, str):
            return self._secrets.sub(lambda _: "[redacted]", value) if self._secrets else value
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise ValueError("Audit detail keys must be strings")
            return {self._redact(key): self._redact(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._redact(item) for item in value]
        return value

    def _detail(self, value: dict) -> str:
        if not isinstance(value, dict):
            raise ValueError("Audit detail must be a JSON object")
        encoded = _json(self._redact(value))
        if len(encoded.encode("utf-8")) <= self.max_bytes:
            return encoded
        # Keep valid JSON, redact before truncating, and bound the encoded UTF-8 bytes.
        preview = encoded.encode("utf-8")[: self.max_bytes].decode("utf-8", errors="ignore")
        low, high = 0, len(preview)
        while low < high:
            middle = (low + high + 1) // 2
            candidate = _json({"truncated": True, "preview": preview[:middle]})
            if len(candidate.encode("utf-8")) <= self.max_bytes:
                low = middle
            else:
                high = middle - 1
        return _json({"truncated": True, "preview": preview[:low]})

    def write(
        self,
        actor: str,
        event: str,
        *,
        run_id: str | None = None,
        chat_id: int | None = None,
        tool: str | None = None,
        decision: str | None = None,
        detail: dict | None = None,
    ) -> int:
        row = {
            "ts": time.time(),
            "actor": actor,
            "event": event,
            "run_id": run_id,
            "chat_id": chat_id,
            "tool": tool,
            "decision": decision,
            "detail": self._detail({} if detail is None else detail),
        }
        for name, limit in (("actor", 100), ("event", 80), ("run_id", 128), ("tool", 80), ("decision", 40)):
            value = row[name]
            if value is not None:
                if not isinstance(value, str):
                    raise ValueError("Audit labels must be strings")
                row[name] = self._redact(value)[:limit]
        if not row["actor"] or not row["event"]:
            raise ValueError("Audit actor and event are required")
        with self.memory.transaction() as db:
            last = db.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
            previous = last[0] if last else FIRST_HASH
            cursor = db.execute(
                "INSERT INTO audit_log(ts,actor,event,run_id,chat_id,tool,decision,detail,prev_hash,hash) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (*[row[name] for name in HASH_FIELDS], previous, _digest(previous, row)),
            )
            return int(cursor.lastrowid)

    def verify(self) -> dict:
        with self.memory._lock:
            return _verify(self.memory._db)


class NullAudit:
    """Tool contexts without an agent don't write audit records."""

    def write(self, actor: str, event: str, **fields) -> None:
        return None

    def verify(self) -> dict:
        return {"ok": True, "rows": 0, "first_bad_id": None}
