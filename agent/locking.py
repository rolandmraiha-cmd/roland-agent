"""Linux file locks held until every database user has finished."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class LockBusy(RuntimeError):
    pass


@contextmanager
def file_lock(path: Path, *, exclusive: bool = True) -> Iterator[None]:
    import fcntl

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("Lock must be a regular file")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise LockBusy("Another process holds the lock") from error
        yield
    finally:
        # Closing releases the lock. Never unlink a lock file: that can create two lock inodes.
        os.close(descriptor)


def database_lock(data_dir: Path, *, exclusive: bool = False):
    """Serve/chat/jobs/backup share this lock; restore requires exclusive access."""
    return file_lock(data_dir / ".serve.lock", exclusive=exclusive)
