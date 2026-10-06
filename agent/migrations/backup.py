"""An optional private snapshot before upgrading an already versioned database."""

from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path


def pre_migration_backup(db: sqlite3.Connection, directory: Path, source: int, target: int) -> Path:
    folder = directory / "db"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    output = folder / f"pre-migrate-v{source}-to-v{target}-{timestamp}.db.gz"
    raw_fd, raw_name = tempfile.mkstemp(dir=folder, suffix=".db")
    os.close(raw_fd)
    packed_name = None
    try:
        destination = sqlite3.connect(raw_name)
        try:
            db.backup(destination)
            result = destination.execute("PRAGMA integrity_check").fetchall()
            if result != [("ok",)]:
                raise ValueError("Pre-migration backup failed integrity_check")
            destination.execute("PRAGMA journal_mode=DELETE")
        finally:
            destination.close()
        packed_fd, packed_name = tempfile.mkstemp(dir=folder, suffix=".gz.tmp")
        with os.fdopen(packed_fd, "wb") as packed:
            with gzip.GzipFile(fileobj=packed, mode="wb") as compressed, open(raw_name, "rb") as raw:
                shutil.copyfileobj(raw, compressed)
            packed.flush()
            os.fsync(packed.fileno())
        os.replace(packed_name, output)
        packed_name = None
        # The final file inherits mkstemp's 0600 mode; make the rename durable too.
        folder_fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(folder_fd)
        finally:
            os.close(folder_fd)
        return output
    finally:
        Path(raw_name).unlink(missing_ok=True)
        for suffix in ("-wal", "-shm", "-journal"):
            Path(raw_name + suffix).unlink(missing_ok=True)
        if packed_name:
            Path(packed_name).unlink(missing_ok=True)
