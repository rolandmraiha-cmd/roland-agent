import asyncio
import gzip
import os
import shutil
import sqlite3
import tarfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent.audit import Audit
from agent.backup import backup_now, prune_backups, restore, workspace_snapshot
from agent.config import Config
from agent.locking import LockBusy, database_lock
from agent.memory import Memory
from agent.migrations import latest_version
from agent.migrations.backup import database_snapshot
from agent.scheduler import run_backup_if_due


def setup(tmp_path, **settings):
    config = Config(
        data_dir=tmp_path / "data", backup_dir=tmp_path / "backups", backup_workspace=False, **settings
    )
    memory = Memory(config.db_path)
    return config, memory, Audit.from_config(memory, config)


def unpack(snapshot, destination):
    with gzip.open(snapshot, "rb") as source, destination.open("wb") as target:
        shutil.copyfileobj(source, target)
    return sqlite3.connect(destination)


def test_backup_is_consistent_during_writes(tmp_path):
    config, memory, audit = setup(tmp_path)
    writer = Memory(config.db_path)
    stop, started = threading.Event(), threading.Event()
    failures = []

    def write():
        try:
            number = 0
            while not stop.is_set():
                with writer.transaction():
                    writer.set_meta("left", str(number))
                    writer.set_meta("right", str(number))
                number += 1
                started.set()
                stop.wait(0.001)
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=write)
    thread.start()
    try:
        assert started.wait(3)
        outputs = backup_now(config, memory, audit)
        assert thread.is_alive()
    finally:
        stop.set()
        thread.join(timeout=3)
        writer.close()
    assert not failures
    with unpack(outputs[0], tmp_path / "copy.db") as restored:
        assert restored.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        values = dict(restored.execute("SELECT key,value FROM meta WHERE key IN ('left','right')"))
        assert values["left"] == values["right"]
    assert outputs[0].stat().st_mode & 0o777 == 0o600
    assert memory.get_meta("last_backup_ok") and memory.get_meta("last_backup_error") == ""
    assert audit.verify()["ok"]
    assert {path.name for path in (config.backup_dir / "db").iterdir()} == {outputs[0].name}
    memory.close()


def test_retention_keeps_dailies_and_weeklies(tmp_path):
    config = Config(backup_dir=tmp_path, backup_keep_daily=14, backup_keep_weekly=8)
    folder = tmp_path / "db"
    folder.mkdir()
    start = datetime(2025, 11, 1, tzinfo=UTC)
    generated = []
    weeks = {}
    for day in range(120):
        date = start + timedelta(days=day)
        path = folder / f"agent-{date:%Y%m%d}-0330.db.gz"
        path.touch()
        generated.append(path)
        weeks[date.isocalendar()[:2]] = path
    extra = folder / f"agent-{date:%Y%m%d}-0200.db.gz"
    extra.touch()
    unrelated = folder / "pre-migrate-v1-to-v2-example.db.gz"
    unrelated.touch()
    outside = tmp_path / "outside"
    outside.write_text("Keep me")
    symlink = folder / "agent-20241101-0330.db.gz"
    symlink.symlink_to(outside)
    prune_backups(config)
    expected = set(generated[-14:]) | set(list(weeks.values())[-8:])
    assert set(folder.iterdir()) == expected | {unrelated, symlink}
    assert 14 <= len(expected) <= 22
    assert all(path.exists() for path in generated[-14:]) and not extra.exists()
    assert outside.read_text() == "Keep me"


def test_workspace_exclusions_symlinks_and_retention(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("Save this")
    for directory in (".trash", ".uploads-tmp", ".sandbox-home/.cache"):
        target = workspace / directory
        target.mkdir(parents=True)
        (target / "excluded.txt").write_text("Do not copy")
    (workspace / ".sandbox-home" / "keep.txt").write_text("Keep home files")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("OUTSIDE CONTENT MUST NOT BE READ")
    (workspace / "directory-link").symlink_to(outside, target_is_directory=True)
    (workspace / "file-link").symlink_to(outside / "secret.txt")
    os.mkfifo(workspace / "fifo")
    output = tmp_path / "backups/workspace/workspace-20261006.tar.gz"
    workspace_snapshot(workspace, output, 1024)
    with tarfile.open(output) as archive:
        assert set(archive.getnames()) == {
            "notes.txt",
            ".sandbox-home",
            ".sandbox-home/keep.txt",
            "directory-link",
            "file-link",
        }
        assert archive.getmember("directory-link").issym() and archive.getmember("file-link").issym()
        assert archive.extractfile("notes.txt").read() == b"Save this"
    assert output.stat().st_mode & 0o777 == 0o600
    for day in range(1, 6):
        (output.parent / f"workspace-2026100{day}.tar.gz").touch()
    prune_backups(Config(backup_dir=output.parents[1]))
    assert {path.name for path in output.parent.iterdir()} == {
        "workspace-20261004.tar.gz",
        "workspace-20261005.tar.gz",
        "workspace-20261006.tar.gz",
    }


@pytest.mark.parametrize("directory", [False, True])
def test_workspace_symlink_swap_cannot_escape(tmp_path, monkeypatch, directory):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("Secret outside workspace")
    target = workspace / "swap"
    if directory:
        target.mkdir()
    else:
        target.write_text("Safe file")
    real_open = os.open
    swapped = False

    def swap(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == "swap" and "dir_fd" in kwargs and not swapped:
            swapped = True
            target.rename(workspace / "old")
            target.symlink_to(outside if directory else outside / "secret.txt", target_is_directory=directory)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr("agent.backup.os.open", swap)
    output = tmp_path / "snapshots/workspace-20261006.tar.gz"
    with pytest.raises(OSError):
        workspace_snapshot(workspace, output, 1024)
    assert swapped and not output.exists() and list(output.parent.iterdir()) == []


def test_hard_links_are_refused(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("Outside")
    os.link(outside, workspace / "link")
    with pytest.raises(ValueError, match="hard links"):
        workspace_snapshot(workspace, tmp_path / "backup.tar.gz", 1024)
    assert not (tmp_path / "backup.tar.gz").exists()


def test_large_workspace_is_skipped_but_database_is_backed_up(tmp_path):
    config = Config(data_dir=tmp_path / "data", backup_dir=tmp_path / "backups", backup_workspace_max_mb=0)
    config.workspace.mkdir(parents=True)
    (config.workspace / "file.txt").write_text("Too large for the zero-byte cap")
    memory = Memory(config.db_path)
    audit = Audit.from_config(memory, config)
    assert len(backup_now(config, memory, audit)) == 1
    assert memory.get_meta("last_backup_ok")
    row = memory._all("SELECT detail FROM audit_log")[0]
    assert '"workspace":"skipped_over_limit"' in row["detail"]
    assert list((config.backup_dir / "workspace").iterdir()) == []


def test_failed_snapshot_preserves_previous_backup_and_reports_error(tmp_path, monkeypatch):
    config, memory, audit = setup(tmp_path)
    outputs = backup_now(config, memory, audit, now=10000)
    original = outputs[0].read_bytes()
    previous = memory.get_meta("last_backup_ok")

    def fail(*args):
        raise OSError("synthetic secret text must not appear in status")

    monkeypatch.setattr("agent.backup.database_snapshot", fail)
    with pytest.raises(OSError):
        backup_now(config, memory, audit, now=10001)
    assert outputs[0].read_bytes() == original
    assert memory.get_meta("last_backup_ok") == previous
    assert memory.get_meta("last_backup_error") == "Backup failed: OSError"
    assert audit.verify()["ok"]
    assert "synthetic secret" not in str(memory._all("SELECT * FROM audit_log"))


def test_restore_refuses_while_serving(tmp_path):
    config, memory, audit = setup(tmp_path)
    memory.remember("Saved fact")
    snapshot = backup_now(config, memory, audit)[0]
    memory.close()
    original = config.db_path.read_bytes()
    with database_lock(config.data_dir):
        with pytest.raises(LockBusy):
            restore(config, snapshot)
    assert config.db_path.read_bytes() == original
    # A retained lock file with no held lock does not falsely block restore.
    assert (config.data_dir / ".serve.lock").exists()
    preserved = restore(config, snapshot)
    assert preserved and preserved.read_bytes() == original
    result = Memory(config.db_path)
    assert [text for _, text in result.facts()] == ["Saved fact"]
    result.close()


def test_restore_keeps_old_data_and_expires_old_cookies(tmp_path):
    config, memory, audit = setup(tmp_path)
    memory.remember("Backup state")
    memory.add_session("synthetic-hash", 10**12)
    snapshot = backup_now(config, memory, audit)[0]
    memory.remember("Added later")
    memory.close()
    preserved = restore(config, snapshot)
    restored = Memory(config.db_path)
    assert [text for _, text in restored.facts()] == ["Backup state"]
    assert restored._all("SELECT * FROM sessions") == []
    assert Audit(restored).verify()["ok"]
    with sqlite3.connect(preserved) as previous:
        assert previous.execute("SELECT text FROM facts ORDER BY id").fetchall() == [
            ("Backup state",),
            ("Added later",),
        ]
    assert config.db_path.stat().st_mode & 0o777 == 0o600
    restored.close()


@pytest.mark.parametrize("content", [b"", b"not SQLite", b"SQLite format 3\x00corrupted"])
def test_invalid_restore_does_not_touch_existing_database(tmp_path, content):
    config, memory, _ = setup(tmp_path)
    memory.remember("Keep this")
    memory.close()
    original = config.db_path.read_bytes()
    archive = tmp_path / "bad.db.gz"
    with gzip.open(archive, "wb") as stream:
        stream.write(content)
    with pytest.raises((ValueError, sqlite3.Error)):
        restore(config, archive)
    assert config.db_path.read_bytes() == original
    assert not list(config.data_dir.glob("*.pre-restore-*"))
    assert not list(config.data_dir.glob("*.restore.db*"))


def test_future_schema_restore_is_refused_without_touching_old_data(tmp_path):
    config, memory, _ = setup(tmp_path)
    memory.remember("Old data")
    memory.close()
    original = config.db_path.read_bytes()
    future = tmp_path / "future.db"
    with sqlite3.connect(future) as db:
        sqlite3.connect(config.db_path).backup(db)
        db.execute("PRAGMA user_version=99")
        snapshot = database_snapshot(db, tmp_path / "future.db.gz")
    with pytest.raises(SystemExit, match="newer"):
        restore(config, snapshot)
    assert config.db_path.read_bytes() == original


def test_restore_runs_migrations_on_the_staged_copy(tmp_path):
    config, memory, _ = setup(tmp_path)
    memory.close()
    baseline = tmp_path / "v1.db"
    with sqlite3.connect(baseline) as db:
        db.executescript((Path(__file__).parent / "fixtures/v1_4fb0950.sql").read_text())
        db.execute("INSERT INTO facts(text,created) VALUES ('V1 fact',1)")
        db.commit()
        snapshot = database_snapshot(db, tmp_path / "v1.db.gz")
    restore(config, snapshot)
    restored = Memory(config.db_path)
    assert restored.schema_version == latest_version() and restored.facts()[0][1] == "V1 fact"
    assert Audit(restored).verify()["ok"]
    restored.close()


@pytest.mark.parametrize("stage", ["preserve_wal", "install"])
def test_restore_rename_failure_puts_original_files_back(tmp_path, monkeypatch, stage):
    config, memory, audit = setup(tmp_path)
    snapshot = backup_now(config, memory, audit)[0]
    memory.close()
    original = config.db_path.read_bytes()
    wal = Path(str(config.db_path) + "-wal")
    wal.write_bytes(b"synthetic sidecar")
    real_rename, real_replace = os.rename, os.replace

    def rename(source, destination):
        if stage == "preserve_wal" and Path(source) == wal:
            raise OSError("Preserve failed")
        return real_rename(source, destination)

    def replace(source, destination):
        if stage == "install" and Path(destination) == config.db_path:
            raise OSError("Install failed")
        return real_replace(source, destination)

    monkeypatch.setattr("agent.backup.os.rename", rename)
    monkeypatch.setattr("agent.backup.os.replace", replace)
    with pytest.raises(OSError):
        restore(config, snapshot)
    assert config.db_path.read_bytes() == original and wal.read_bytes() == b"synthetic sidecar"
    assert not list(config.data_dir.glob("*.pre-restore-*"))
    assert not list(config.data_dir.glob("*.restore.db*"))


async def test_backup_schedule_uses_local_day_and_retries_hourly(tmp_path, make_agent, monkeypatch):
    agent = make_agent(backup_dir=tmp_path / "backups")
    calls = []

    def fake(config, memory, audit, *, now):
        calls.append(now)
        if len(calls) == 1:
            raise OSError("First attempt fails")
        memory.set_meta("last_backup_ok", str(now))

    monkeypatch.setattr("agent.backup.backup_now", fake)
    before = datetime.fromisoformat("2026-10-06T03:29:00+03:00").timestamp()
    assert not await run_backup_if_due(agent, before)
    with pytest.raises(OSError):
        await run_backup_if_due(agent, before + 60)
    assert not await run_backup_if_due(agent, before + 120)
    assert await run_backup_if_due(agent, before + 3660)
    assert not await run_backup_if_due(agent, before + 7200)
    assert await run_backup_if_due(agent, before + 86400 + 60)
    assert len(calls) == 3


async def test_dst_skipped_and_repeated_hours_back_up_once_per_day(tmp_path, make_agent, monkeypatch):
    agent = make_agent(backup_dir=tmp_path / "backups")
    calls = []

    def fake(config, memory, audit, *, now):
        calls.append(now)
        memory.set_meta("last_backup_ok", str(now))

    monkeypatch.setattr("agent.backup.backup_now", fake)
    for stamp in ("2026-03-29T04:00:00+03:00", "2026-10-25T03:30:00+03:00"):
        assert await run_backup_if_due(agent, datetime.fromisoformat(stamp).timestamp())
    assert not await run_backup_if_due(agent, datetime.fromisoformat("2026-10-25T03:30:00+02:00").timestamp())
    assert len(calls) == 2


async def test_shutdown_waits_for_the_backup_thread(tmp_path, make_agent, monkeypatch):
    agent = make_agent(backup_dir=tmp_path / "backups")
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def fake(*args, **kwargs):
        started.set()
        assert release.wait(3)
        finished.set()

    monkeypatch.setattr("agent.backup.backup_now", fake)
    now = datetime.fromisoformat("2026-10-06T04:00:00+03:00").timestamp()
    task = asyncio.create_task(run_backup_if_due(agent, now))
    assert await asyncio.to_thread(started.wait, 3)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


def test_unversioned_v1_is_snapshotted_before_its_first_upgrade(tmp_path):
    old = tmp_path / "old.db"
    folder = tmp_path / "backups"
    folder.mkdir()
    with sqlite3.connect(old) as db:
        db.executescript((Path(__file__).parent / "fixtures/v1_4fb0950.sql").read_text())
        db.execute("INSERT INTO facts(text,created) VALUES ('Before upgrade',1)")
    memory = Memory(old, backup_dir=folder)
    snapshots = list((folder / "db").glob(f"pre-migrate-v0-to-v{latest_version()}-*.db.gz"))
    assert len(snapshots) == 1
    with unpack(snapshots[0], tmp_path / "before.db") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert db.execute("SELECT text FROM facts").fetchone()[0] == "Before upgrade"
        assert "origin" not in {row[1] for row in db.execute("PRAGMA table_info(facts)")}
    assert memory.schema_version == latest_version()
    memory.close()


def test_restore_preserves_an_unclean_database_with_its_wal(tmp_path):
    import subprocess
    import sys

    config, memory, audit = setup(tmp_path)
    memory.remember("Snapshot fact")
    snapshot = backup_now(config, memory, audit)[0]
    memory.close()
    script = """
import os
import sys
from agent.memory import Memory
memory = Memory(sys.argv[1])
memory.remember('Uncheckpointed fact')
os._exit(0)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(config.db_path)],
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
    )
    assert result.returncode == 0
    assert Path(str(config.db_path) + "-wal").exists()
    preserved = restore(config, snapshot)
    with sqlite3.connect(preserved) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT text FROM facts ORDER BY id").fetchall() == [
            ("Snapshot fact",),
            ("Uncheckpointed fact",),
        ]
    restored = Memory(config.db_path)
    assert [text for _, text in restored.facts()] == ["Snapshot fact"]
    restored.close()
