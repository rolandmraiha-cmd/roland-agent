"""Exercise the restore drill with the real built core image and synthetic data."""

from __future__ import annotations

import gzip
import os
import subprocess
import tempfile
from pathlib import Path

from agent.audit import Audit
from agent.backup import backup_now
from agent.config import Config
from agent.memory import Memory

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    if os.getenv("GITHUB_ACTIONS") != "true":
        raise SystemExit("The container restore probe is restricted to disposable GitHub CI")
    with tempfile.TemporaryDirectory(prefix="roland-restore-ci-") as folder:
        path = Path(folder)
        config = Config(data_dir=path / "data", backup_dir=path / "backups", backup_workspace=False)
        memory = Memory(config.db_path)
        try:
            memory.remember("Synthetic restore-drill fact")
            memory.add_session("synthetic-expired-by-restore", 10**12)
            snapshot = backup_now(config, memory, Audit(memory))[0]
        finally:
            memory.close()
        original = snapshot.read_bytes()
        for source, expected in ((snapshot, True), (path / "bad.db.gz", False)):
            if not expected:
                with gzip.open(source, "wb") as stream:
                    stream.write(b"not a database")
            result = subprocess.run(
                ["bash", str(ROOT / "deploy/restore.sh"), "--test"],
                cwd=ROOT,
                env={**os.environ, "APPLY": "1", "FILE": str(source)},
                capture_output=True,
                text=True,
                timeout=180,
            )
            print(result.stdout, end="")
            if (result.returncode == 0) != expected:
                raise SystemExit("Unexpected restore-drill result: " + result.stderr)
        if snapshot.read_bytes() != original:
            raise SystemExit("Restore drill modified its input")
    print("Real-container restore and corrupt-backup refusal: PASS")


if __name__ == "__main__":
    main()
