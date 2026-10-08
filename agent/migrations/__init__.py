"""Forward-only SQLite migrations, each committed together with its version."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import m0001_v1_baseline, m0002_v2_core, m0003_model_training

MIGRATIONS = [(1, m0001_v1_baseline.apply), (2, m0002_v2_core.apply), (3, m0003_model_training.apply)]


def latest_version() -> int:
    return MIGRATIONS[-1][0]


def current_version(db: sqlite3.Connection) -> int:
    return int(db.execute("PRAGMA user_version").fetchone()[0])


def inspect_version(path: Path) -> int:
    """Read an existing database without creating it or applying migrations."""
    if not path.exists():
        return 0
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return current_version(db)
    finally:
        db.close()


def migrate(db: sqlite3.Connection) -> None:
    versions = [version for version, _ in MIGRATIONS]
    if versions != list(range(1, latest_version() + 1)):
        raise SystemExit("Invalid migration sequence; versions must be consecutive from 1")
    for version, apply in MIGRATIONS:
        try:
            db.execute("BEGIN IMMEDIATE")
            # Another process may have upgraded while this one was waiting for the lock.
            current = current_version(db)
            if current > latest_version():
                raise ValueError(
                    f"database version {current} is newer than supported version {latest_version()}"
                )
            if current < version:
                apply(db)
                db.execute(f"PRAGMA user_version = {version}")
            db.commit()
        except Exception as error:
            db.rollback()
            raise SystemExit(f"migration {version} failed: {error}") from error
        except BaseException:
            db.rollback()
            raise
