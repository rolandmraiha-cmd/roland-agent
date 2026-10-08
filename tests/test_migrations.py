"""Upgrade real v1 shapes, preserve data, and roll back failed schema changes."""

import gzip
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from agent import migrations
from agent.memory import Memory
from agent.migrations.m0001_v1_baseline import SCHEMA

V1_SCHEMA = Path(__file__).with_name("fixtures").joinpath("v1_4fb0950.sql").read_text()


def v1_database(path, version=0):
    db = sqlite3.connect(path)
    db.executescript(V1_SCHEMA)
    db.execute("INSERT INTO chats VALUES (1, 'Saved chat', 'chat', 100, 200)")
    db.execute("INSERT INTO messages VALUES (1, 1, 'user', 'Saved message', 150)")
    db.execute("INSERT INTO facts VALUES (1, 'Saved fact', 150)")
    db.execute(
        "INSERT INTO jobs VALUES (1, 'Agent job', '* * * * *', 'saved prompt', 0, 0, 'agent', 300, 100)"
    )
    db.execute(
        "INSERT INTO jobs VALUES (2, 'Panel job', '* * * * *', 'panel prompt', 1, 1, 'panel', 300, 100)"
    )
    db.execute("INSERT INTO job_runs VALUES (1, 2, 'Panel job', 300, 301, 1, 'Saved result')")
    db.execute("INSERT INTO usage VALUES ('2026-01-01', 7)")
    db.execute("INSERT INTO sessions VALUES ('session-test-hash', 800, 9000000000, 900)")
    db.execute("INSERT INTO meta VALUES ('saved-setting', 'saved-value')")
    db.execute(f"PRAGMA user_version = {version}")
    db.commit()
    db.close()


def database_state(path):
    db = sqlite3.connect(path)
    try:
        schema = db.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
        version = migrations.current_version(db)
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        contents = {table: db.execute(f'SELECT * FROM "{table}"').fetchall() for table in tables}
        return version, schema, contents
    finally:
        db.close()


def test_baseline_schema_is_exact_v1_fixture():
    assert SCHEMA == V1_SCHEMA.split("\n", 1)[1]


def test_fresh_db_reaches_latest_version(tmp_path):
    path = tmp_path / "agent.db"
    memory = Memory(path)
    try:
        assert memory.schema_version == migrations.latest_version() == 3
        tables = {row[0] for row in memory._all("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {
            "runs",
            "approvals",
            "audit_log",
            "files",
            "trash",
            "signin_requests",
            "screen_sessions",
        } <= tables
        assert memory._all("PRAGMA foreign_keys")[0][0] == 1
        assert memory._all("PRAGMA busy_timeout")[0][0] == 10000
        assert memory._all("PRAGMA synchronous")[0][0] == 1
        assert memory._all("PRAGMA journal_mode")[0][0] == "wal"
        assert path.stat().st_mode & 0o777 == 0o600
        assert memory._all("PRAGMA integrity_check")[0][0] == "ok"
        assert memory._all("PRAGMA foreign_key_check") == []
    finally:
        memory.close()


@pytest.mark.parametrize("version", [0, 1])
def test_v1_db_at_4fb0950_migrates_and_keeps_data(tmp_path, version):
    path = tmp_path / "v1.db"
    v1_database(path, version)
    memory = Memory(path)
    try:
        assert memory.schema_version == migrations.latest_version()
        assert memory.chats() == [{"id": 1, "title": "Saved chat", "updated": 200}]
        assert memory.messages(1) == [{"role": "user", "content": "Saved message", "created": 150}]
        assert memory.facts() == [(1, "Saved fact")]
        assert memory.facts_detailed()[0]["origin"] == "unknown"
        assert memory.facts_detailed()[0]["tainted"] is False
        assert [(job.id, job.approved, job.origin) for job in memory.jobs()] == [
            (1, False, "agent"),
            (2, True, "panel"),
        ]
        assert memory.runs()[0]["output"] == "Saved result"
        assert memory.calls_today("2026-01-01") == 7
        assert memory.get_meta("saved-setting") == "saved-value"
        assert memory._all("SELECT last_seen FROM sessions")[0][0] == 900
        assert memory.session_valid("session-test-hash", 1000, 3600)
        assert memory.new_chat("Next chat") == 2
        assert memory._all("PRAGMA integrity_check")[0][0] == "ok"
    finally:
        memory.close()


def pre_approval_database(path):
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            cron TEXT NOT NULL, prompt TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
            next_run REAL NOT NULL, created REAL NOT NULL);
        CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, created REAL NOT NULL, expires REAL NOT NULL);
        INSERT INTO jobs VALUES (1, 'Old job', '* * * * *', 'old prompt', 1, 200, 100);
        INSERT INTO sessions VALUES ('old-session-test-hash', 100, 9000000000);
    """)
    db.close()


def test_pre_approval_db_migrates(tmp_path):
    path = tmp_path / "old.db"
    pre_approval_database(path)
    memory = Memory(path)
    try:
        assert memory.schema_version == 3
        job = memory.job(1)
        assert job.prompt == "old prompt" and job.origin == "old" and not job.approved
        assert memory.due_jobs(1000) == []
        assert not memory.session_valid("old-session-test-hash", 200, 3600)
    finally:
        memory.close()


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "agent.db"
    memory = Memory(path)
    memory.remember("keep this")
    memory.close()
    before = database_state(path)

    def broken(db):
        db.execute("CREATE TABLE broken_table (id INTEGER)")
        db.execute("ALTER TABLE facts ADD COLUMN broken_column TEXT")
        db.execute("UPDATE facts SET text = 'discard this edit'")
        raise RuntimeError("deliberate failure")

    monkeypatch.setattr(migrations, "MIGRATIONS", [*migrations.MIGRATIONS, (4, broken)])
    with pytest.raises(SystemExit, match="migration 4 failed: deliberate failure"):
        Memory(path)
    assert database_state(path) == before


def test_failed_baseline_restores_old_sessions_and_jobs(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    pre_approval_database(path)
    before = database_state(path)
    original = migrations.MIGRATIONS[0][1]

    def broken(db):
        original(db)
        raise RuntimeError("deliberate failure")

    monkeypatch.setattr(migrations, "MIGRATIONS", [(1, broken), migrations.MIGRATIONS[1]])
    with pytest.raises(SystemExit, match="migration 1 failed"):
        Memory(path)
    assert database_state(path) == before


def test_failed_second_migration_keeps_completed_baseline(tmp_path, monkeypatch):
    path = tmp_path / "agent.db"

    def broken(db):
        db.execute("CREATE TABLE broken_table (id INTEGER)")
        raise RuntimeError("deliberate failure")

    monkeypatch.setattr(migrations, "MIGRATIONS", [(1, migrations.MIGRATIONS[0][1]), (2, broken)])
    with pytest.raises(SystemExit, match="migration 2 failed"):
        Memory(path)
    version, schema, _ = database_state(path)
    assert version == 1
    assert "facts" in {row[0] for row in schema}
    assert "broken_table" not in {row[0] for row in schema}


def test_reopening_latest_db_does_not_reapply_migrations(tmp_path):
    path = tmp_path / "agent.db"
    memory = Memory(path)
    memory.remember("keep this")
    memory.close()
    before = database_state(path)
    reopened = Memory(path)
    reopened.close()
    assert database_state(path) == before


def test_future_schema_version_is_refused_without_data_changes(tmp_path):
    path = tmp_path / "agent.db"
    memory = Memory(path)
    memory._exec("PRAGMA user_version = 99")
    memory.close()
    before = database_state(path)
    with pytest.raises(SystemExit, match="database version 99 is newer than supported version 3"):
        Memory(path)
    assert database_state(path) == before


def test_two_initializers_share_upgrade_safely(tmp_path):
    path = tmp_path / "v1.db"
    v1_database(path, version=1)
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.close()
    barrier = Barrier(2, timeout=10)

    def start():
        barrier.wait()
        memory = Memory(path)
        try:
            return memory.schema_version, memory.facts()
        finally:
            memory.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(start), pool.submit(start)
        assert first.result(timeout=15) == second.result(timeout=15) == (3, [(1, "Saved fact")])


def test_pre_migration_backup_keeps_previous_version(tmp_path):
    path = tmp_path / "v1.db"
    v1_database(path, version=1)
    backups = tmp_path / "backups"
    backups.mkdir()
    memory = Memory(path, backup_dir=backups)
    memory.close()
    files = list((backups / "db").iterdir())
    assert len(files) == 1 and files[0].name.startswith("pre-migrate-v1-to-v3-")
    assert files[0].stat().st_mode & 0o777 == 0o600
    restored = tmp_path / "snapshot.db"
    restored.write_bytes(gzip.decompress(files[0].read_bytes()))
    version, schema, contents = database_state(restored)
    assert version == 1
    assert "approvals" not in {row[0] for row in schema}
    assert contents["facts"] == [(1, "Saved fact", 150)]
    assert migrations.inspect_version(path) == 3


def test_failed_pre_migration_backup_stops_upgrade(tmp_path, monkeypatch):
    import agent.memory as module

    path = tmp_path / "v1.db"
    v1_database(path, version=1)
    backups = tmp_path / "backups"
    backups.mkdir()
    before = database_state(path)

    def fail(*args):
        raise OSError("deliberate backup failure")

    monkeypatch.setattr(module, "pre_migration_backup", fail)
    with pytest.raises(OSError, match="deliberate backup failure"):
        Memory(path, backup_dir=backups)
    assert database_state(path) == before


def test_in_memory_database_migrates_without_wal():
    memory = Memory(":memory:")
    try:
        assert memory.schema_version == 3
        assert memory._all("PRAGMA journal_mode")[0][0] == "memory"
    finally:
        memory.close()


@pytest.mark.parametrize("exists", [False, True])
def test_migrate_check_does_not_create_or_upgrade_database(tmp_path, exists):
    folder = tmp_path / "data"
    path = folder / "agent.db"
    before = None
    if exists:
        folder.mkdir()
        v1_database(path)
        before = database_state(path)
    environment = dict(
        os.environ,
        DATA_DIR=str(folder),
        MODEL_BASE_URL="https://8.8.8.8",
        MODEL_PROVIDER="unsupported",
        AGENT_PASSWORD_HASH="",
        PYTHON_DOTENV_DISABLED="1",
    )
    result = subprocess.run(
        [sys.executable, "-m", "agent", "migrate", "--check"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Database version: 0; target: 3; upgrade pending" in result.stdout
    if exists:
        assert database_state(path) == before
    else:
        assert not folder.exists()
