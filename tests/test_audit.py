import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.audit import FIRST_HASH, Audit, NullAudit, verify_file
from agent.memory import Memory
from agent.tools import ToolContext, schedule_job
from agent.web.app import create_app


def rows(memory):
    return [dict(row) for row in memory._all("SELECT * FROM audit_log ORDER BY id")]


def test_chain_verifies(tmp_path):
    memory = Memory(tmp_path / "agent.db")
    audit = Audit(memory)
    audit.write("system", "startup")
    audit.write("roland", "job_created", chat_id=3, detail={"job_id": 1, "name": "Yö"})
    previous = FIRST_HASH
    for row in rows(memory):
        document = {
            key: row[key] for key in ("ts", "actor", "event", "run_id", "chat_id", "tool", "decision")
        }
        document["detail"] = json.loads(row["detail"])
        encoded = json.dumps(
            document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
        expected = hashlib.sha256((previous + encoded).encode()).hexdigest()
        assert row["prev_hash"] == previous and row["hash"] == expected
        previous = expected
    assert audit.verify() == {"ok": True, "rows": 2, "first_bad_id": None}
    memory.close()
    assert verify_file(tmp_path / "agent.db")["ok"]


@pytest.mark.parametrize("statement", ["UPDATE audit_log SET event='changed'", "DELETE FROM audit_log"])
def test_update_and_delete_are_blocked(tmp_path, statement):
    memory = Memory(tmp_path / "agent.db")
    audit = Audit(memory)
    audit.write("system", "startup")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        memory._exec(statement)
    audit.write("system", "backup")
    assert audit.verify()["rows"] == 2


@pytest.mark.parametrize("change", ["detail='{}'", "prev_hash='bad'", "hash='bad'", "actor='changed'"])
def test_tamper_detected(tmp_path, change):
    memory = Memory(tmp_path / "agent.db")
    audit = Audit(memory)
    audit.write("system", "startup", detail={"source": "test"})
    audit.write("system", "backup", detail={"source": "test"})
    copy = tmp_path / "copy.db"
    with sqlite3.connect(copy) as db:
        memory._db.backup(db)
        db.execute("DROP TRIGGER audit_no_update")
        db.execute(f"UPDATE audit_log SET {change} WHERE id=2")
    assert verify_file(copy) == {"ok": False, "rows": 2, "first_bad_id": 2}
    assert audit.verify()["ok"]


def test_secrets_redacted(make_agent):
    agent = make_agent()
    settings = {
        field.name: f'test-secret-{field.name}-"-\\-ä\nend'
        for field in fields(agent.config)
        if field.metadata.get("secret")
    }
    audit = Audit.from_config(agent.memory, replace(agent.config, **settings))
    secrets = list(settings.values())
    audit.write(
        "agent",
        "tool_call",
        tool=secrets[0],
        detail={
            "nested": [{value: "Bearer " + value} for value in secrets],
            "tuple": tuple(secrets),
            "normal": "Keep this",
            "value": 1,
        },
    )
    row = rows(agent.memory)[0]
    assert row["tool"] == "[redacted]"
    detail = json.loads(row["detail"])
    assert detail["normal"] == "Keep this" and detail["value"] == 1
    assert detail["tuple"] == ["[redacted]"] * len(secrets)
    assert detail["nested"] == [{"[redacted]": "Bearer [redacted]"}] * len(secrets)
    assert audit.verify()["ok"]
    assert all(value not in json.dumps(row, ensure_ascii=False) for value in secrets)


@pytest.mark.parametrize("text", ["ää😀" * 1000, '\\"\n' * 1000])
def test_detail_byte_cap_keeps_valid_json_and_redacts_before_cutting(tmp_path, text):
    memory = Memory(tmp_path / "agent.db")
    audit = Audit(memory, ["private-secret"], max_bytes=128)
    audit.write("system", "note", detail={"a": "private-secret", "b": text})
    encoded = rows(memory)[0]["detail"]
    assert len(encoded.encode()) <= 128
    detail = json.loads(encoded)
    assert detail["truncated"] and "[redacted]" in detail["preview"]
    assert "private-secret" not in encoded
    assert audit.verify()["ok"]


def test_concurrent_writers_share_one_chain(tmp_path):
    first = Memory(tmp_path / "agent.db")
    second = Memory(tmp_path / "agent.db")
    audits = [Audit(first), Audit(second)]

    def write(index):
        for number in range(40):
            audits[index].write("system", "backup", detail={"writer": index, "number": number})

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(write, range(2)))
    assert audits[0].verify() == {"ok": True, "rows": 80, "first_bad_id": None}
    assert len({row["hash"] for row in rows(first)}) == 80
    first.close()
    second.close()


def test_related_state_and_audit_roll_back_together(tmp_path):
    memory = Memory(tmp_path / "agent.db")
    audit = Audit(memory)
    with pytest.raises(RuntimeError, match="cancel"):
        with memory.transaction():
            memory.remember("Temporary fact")
            audit.write("roland", "fact_deleted")
            raise RuntimeError("cancel")
    assert memory.facts() == [] and rows(memory) == []
    with memory.transaction():
        memory.remember("Keep this fact")
        with pytest.raises(ValueError):
            with memory.transaction():
                memory.remember("Discard this fact")
                raise ValueError("inner rollback")
        audit.write("system", "backup")
    assert [text for _, text in memory.facts()] == ["Keep this fact"]
    assert audit.verify()["rows"] == 1


@pytest.mark.parametrize("value", [[], "text", {"bad": float("nan")}, {1: "value"}])
def test_invalid_detail_never_writes_a_row(tmp_path, value):
    memory = Memory(tmp_path / "agent.db")
    audit = Audit(memory)
    with pytest.raises(ValueError):
        audit.write("system", "backup", detail=value)
    assert audit.verify()["rows"] == 0


async def test_agent_wires_real_audit_but_bare_context_defaults_to_null(make_agent):
    agent = make_agent()
    assert agent.ctx.audit is agent.audit and isinstance(agent.audit, Audit)
    context = ToolContext(agent.memory, agent.config.workspace, agent.config.timezone, False)
    assert isinstance(context.audit, NullAudit)
    await schedule_job(agent.ctx, {"name": "Test", "cron": "0 7 * * *", "prompt": "Synthetic job"})
    assert rows(agent.memory)[0]["event"] == "job_created"
    assert json.loads(rows(agent.memory)[0]["detail"])["approved"] is False


async def _no_sleep(delay):
    pass


def test_existing_endpoint_events_without_password_or_cookie(make_agent, monkeypatch):
    monkeypatch.setattr("agent.web.app.asyncio.sleep", _no_sleep)
    agent = make_agent()
    with TestClient(create_app(agent, run_scheduler=False)) as client:
        origin = {"Origin": "http://testserver"}
        assert (
            client.post("/login", data={"password": "synthetic-wrong-password"}, headers=origin).status_code
            == 401
        )
        assert (
            client.post(
                "/login",
                data={"password": "correct horse battery staple"},
                headers=origin,
                follow_redirects=False,
            ).status_code
            == 303
        )
        token = client.cookies.get("agent_session")
        headers = {**origin, "X-CSRF-Token": client.get("/api/status").json()["csrf"]}
        job = client.post(
            "/api/jobs", json={"name": "Test", "cron": "0 7 * * *", "prompt": "test"}, headers=headers
        ).json()["id"]
        assert client.post(f"/api/jobs/{job}/approve", headers=headers).status_code == 200
        fact = agent.memory.remember("Synthetic fact")
        assert client.delete(f"/api/facts/{fact}", headers=headers).status_code == 200
        assert client.delete(f"/api/jobs/{job}", headers=headers).status_code == 200
        assert client.delete(f"/api/jobs/{job}", headers=headers).status_code == 404
        assert client.post("/logout", headers=headers).status_code == 200
    events = rows(agent.memory)
    assert [row["event"] for row in events] == [
        "startup",
        "login_fail",
        "login_ok",
        "job_created",
        "job_approved",
        "fact_deleted",
        "job_deleted",
        "logout",
    ]
    encoded = json.dumps(events)
    assert token not in encoded and "synthetic-wrong-password" not in encoded
    assert "correct horse battery staple" not in encoded and agent.config.password_hash not in encoded
    assert agent.audit.verify()["ok"]


@pytest.mark.parametrize(
    "event", ["login_ok", "job_created", "job_approved", "job_deleted", "fact_deleted", "logout"]
)
def test_audit_failure_prevents_endpoint_state_change(make_agent, event):
    agent = make_agent()
    with TestClient(create_app(agent, run_scheduler=False), raise_server_exceptions=False) as client:
        origin = {"Origin": "http://testserver"}
        if event != "login_ok":
            client.post("/login", data={"password": "correct horse battery staple"}, headers=origin)
            origin["X-CSRF-Token"] = client.get("/api/status").json()["csrf"]
        job = agent.memory.add_job("Test", "0 7 * * *", "test", 0, approved=False)
        fact = agent.memory.remember("Test fact")
        before = {
            table: [tuple(row) for row in agent.memory._all(f"SELECT * FROM {table}")]
            for table in ("sessions", "jobs", "facts", "audit_log")
        }
        agent.memory._exec(
            f"CREATE TRIGGER test_fail_audit BEFORE INSERT ON audit_log "
            f"WHEN NEW.event='{event}' BEGIN SELECT RAISE(ABORT, 'test failure'); END"
        )
        if event == "login_ok":
            response = client.post(
                "/login", data={"password": "correct horse battery staple"}, headers=origin
            )
            assert "set-cookie" not in response.headers
        elif event == "job_created":
            response = client.post(
                "/api/jobs", json={"name": "Other", "cron": "0 8 * * *", "prompt": "other"}, headers=origin
            )
        elif event == "job_approved":
            response = client.post(f"/api/jobs/{job}/approve", headers=origin)
        elif event == "job_deleted":
            response = client.delete(f"/api/jobs/{job}", headers=origin)
        elif event == "fact_deleted":
            response = client.delete(f"/api/facts/{fact}", headers=origin)
        else:
            response = client.post("/logout", headers=origin)
        assert response.status_code == 500
        for table, original in before.items():
            assert [tuple(row) for row in agent.memory._all(f"SELECT * FROM {table}")] == original


def test_verify_cli_is_read_only_and_uses_no_model(tmp_path):
    source = Path(__file__).resolve().parents[1]
    environment = {
        **os.environ,
        "PYTHONPATH": str(source),
        "DATA_DIR": str(tmp_path),
        "MODEL_PROVIDER": "invalid",
    }
    command = [sys.executable, "-m", "agent", "audit-verify"]
    missing = subprocess.run(command, cwd=tmp_path, env=environment, capture_output=True, text=True)
    assert missing.returncode != 0 and not (tmp_path / "agent.db").exists()
    memory = Memory(tmp_path / "agent.db")
    Audit(memory).write("system", "startup")
    memory.close()
    result = subprocess.run(command, cwd=tmp_path, env=environment, capture_output=True, text=True)
    assert result.returncode == 0 and json.loads(result.stdout)["rows"] == 1
    with sqlite3.connect(tmp_path / "agent.db") as db:
        db.execute("DROP TRIGGER audit_no_update")
        db.execute("UPDATE audit_log SET detail='invalid json'")
    result = subprocess.run(command, cwd=tmp_path, env=environment, capture_output=True, text=True)
    assert result.returncode == 1 and json.loads(result.stdout)["first_bad_id"] == 1


def test_audit_rows_cap_matches_csv_export_default(tmp_path):
    """Memory hard-cap must allow the export route default of 5000 rows."""
    memory = Memory(tmp_path / "agent.db")
    audit = Audit(memory)
    for i in range(510):
        audit.write("system", "note", detail={"i": i})
    # Cap used to be 500; export.csv defaults to 5000 — keep them aligned.
    assert len(memory.audit_rows(limit=5000)) == 510
    assert len(memory.audit_rows(limit=100)) == 100
    # Still harden against absurd limits.
    assert len(memory.audit_rows(limit=99_999)) == 510
