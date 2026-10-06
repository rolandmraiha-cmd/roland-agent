"""Persistence invariants for the later approval, file and screen features."""

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from agent.memory import Memory


@pytest.fixture
def memory(tmp_path):
    value = Memory(tmp_path / "agent.db")
    try:
        yield value
    finally:
        value.close()


def approval(memory, *, category="shell", expires=1000, needs_confirm=False):
    chat_id = memory.new_chat()
    run_id = memory.add_agent_run("chat", chat_id=chat_id)
    identifier = memory.add_approval(
        run_id,
        "run_shell",
        {"z": 1, "command": "echo test"},
        category,
        "Fixture action",
        {"command": "echo test"},
        expires,
        chat_id=chat_id,
        needs_confirm=needs_confirm,
    )
    return memory.approval(identifier)


def test_timeline_events_do_not_enter_conversation_history(memory):
    chat_id = memory.new_chat()
    memory.add_message(chat_id, "user", "hello")
    memory.add_event(chat_id, "approval", "Needs review", {"approval_id": "fixture"}, "fixture-run")
    memory.add_message(chat_id, "assistant", "reply")
    assert [row["content"] for row in memory.messages(chat_id)] == ["hello", "reply"]
    assert [row["content"] for row in memory.messages(chat_id, limit=1)] == ["reply"]
    timeline = memory.timeline(chat_id)
    assert [row["kind"] for row in timeline] == ["text", "approval", "text"]
    assert timeline[1]["meta"] == {"approval_id": "fixture"}
    assert timeline[1]["run_id"] == "fixture-run"


@pytest.mark.parametrize("metadata", [{"x": "é" * 8192}, {"x": float("nan")}, [1, 2]])
def test_invalid_event_metadata_leaves_no_partial_event(memory, metadata):
    chat_id = memory.new_chat()
    with pytest.raises(ValueError):
        memory.add_event(chat_id, "note", "invalid", metadata)
    assert memory.timeline(chat_id) == []


def test_deleted_chat_keeps_run_record_but_removes_events(memory):
    chat_id = memory.new_chat()
    run_id = memory.add_agent_run("chat", chat_id=chat_id)
    memory.add_event(chat_id, "note", "event", run_id=run_id)
    assert memory.delete_chat(chat_id)
    assert memory.timeline(chat_id) == []
    assert memory.agent_run(run_id)["chat_id"] is None


def test_fact_origin_and_taint_do_not_change_v1_shape(memory):
    chat_id = memory.new_chat()
    identifier = memory.remember("saved fact", origin="agent", chat_id=chat_id, tainted=True)
    assert memory.facts() == [(identifier, "saved fact")]
    detailed = memory.facts_detailed()[0]
    assert detailed["origin"] == "agent" and detailed["chat_id"] == chat_id and detailed["tainted"] is True
    assert memory.remember("saved fact", origin="panel", tainted=False) == identifier
    assert memory.facts_detailed()[0] == detailed


def test_v2_runs_leave_job_run_api_compatible(memory):
    job_id = memory.add_job("Job", "* * * * *", "fixture", 0)
    job_run_id = memory.start_run(memory.job(job_id))
    identifier = memory.add_agent_run("job", job_id=job_id, job_run_id=job_run_id)
    assert memory.set_run_tainted(identifier)
    assert memory.finish_agent_run(identifier, "stopped")
    assert not memory.finish_agent_run(identifier)
    assert memory.agent_run(identifier)["tainted"] is True
    assert memory.agent_run(identifier)["status"] == "stopped"
    assert memory.runs()[0]["id"] == job_run_id
    assert memory.runs()[0]["finished"] is None


def test_approval_is_bound_to_canonical_arguments(memory):
    row = approval(memory)
    encoded = '{"command":"echo test","z":1}'
    assert row["args_json"] == encoded
    assert row["args_hash"] == hashlib.sha256(encoded.encode()).hexdigest()
    assert row["details"] == {"command": "echo test"}
    assert not memory.decide_approval(row["id"], "approved", "wrong-hash", now=500)
    assert memory.approval(row["id"])["status"] == "pending"
    assert memory.decide_approval(row["id"], "approved", row["args_hash"], now=500)
    assert not memory.decide_approval(row["id"], "approved", row["args_hash"], now=501)
    assert not memory.delete_approval(row["id"])
    assert memory.finish_approval(row["id"], True, "fixture result")
    assert not memory.finish_approval(row["id"], True, "second result")
    assert memory.approval(row["id"])["result_digest"] == hashlib.sha256(b"fixture result").hexdigest()


@pytest.mark.parametrize("category", ["payment", "message", "public_post", "delete"])
def test_sensitive_categories_require_confirmation(memory, category):
    row = approval(memory, category=category)
    assert row["needs_confirm"] is True
    assert not memory.decide_approval(row["id"], "approved", row["args_hash"], now=500)
    assert memory.decide_approval(row["id"], "approved", row["args_hash"], confirm=True, now=500)


def test_expired_or_rejected_action_cannot_execute(memory):
    expired = approval(memory, expires=500)
    assert not memory.decide_approval(expired["id"], "approved", expired["args_hash"], now=500)
    assert memory.expire_approvals(500) == 1
    assert not memory.finish_approval(expired["id"], True, "result")
    rejected = approval(memory, category="payment")
    assert memory.decide_approval(rejected["id"], "rejected", rejected["args_hash"], note="x" * 600, now=500)
    assert len(memory.approval(rejected["id"])["decision_note"]) == 500
    assert not memory.finish_approval(rejected["id"], True, "result")
    assert memory.delete_approval(rejected["id"])
    assert memory.approval(rejected["id"]) is None


def test_pending_approval_can_be_cancelled_without_deletion(memory):
    row = approval(memory)
    assert not memory.delete_approval(row["id"])
    assert memory.cancel_approval(row["id"])
    assert memory.approval(row["id"])["status"] == "cancelled"
    assert memory.approvals() == []


def test_simultaneous_decisions_have_one_winner(tmp_path):
    first, second = Memory(tmp_path / "agent.db"), Memory(tmp_path / "agent.db")
    row = approval(first)
    ready = Barrier(2, timeout=10)

    def decide(memory):
        ready.wait()
        return memory.decide_approval(row["id"], "approved", row["args_hash"], now=500)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(decide, first), pool.submit(decide, second)
            assert sorted([a.result(timeout=15), b.result(timeout=15)]) == [False, True]
    finally:
        first.close()
        second.close()


def test_audit_schema_is_append_only_and_failed_writes_roll_back(memory):
    memory._exec(
        "INSERT INTO audit_log(ts, actor, event, detail, prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?)",
        (100, "system", "fixture", "{}", "0" * 64, "1" * 64),
    )
    for sql in ("UPDATE audit_log SET event = 'changed'", "DELETE FROM audit_log"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            memory._exec(sql)
        assert not memory._db.in_transaction
    assert memory._all("SELECT event FROM audit_log")[0][0] == "fixture"
    assert memory.new_chat() == 1


def test_file_and_trash_helpers_only_change_metadata(memory, tmp_path, monkeypatch):
    import agent.memory as module

    now = [100]
    monkeypatch.setattr(module.time, "time", lambda: now[0])
    memory.record_file("folder//a.txt", 10, "digest", origin="upload")
    now[0] = 200
    memory.record_file("folder/a.txt", 20, "new-digest", origin="agent")
    row = memory.file("folder/a.txt")
    assert row["created"] == 100 and row["updated"] == 200 and row["size"] == 20
    trash_id = memory.add_trash("folder/a.txt", ".trash/fixture/a.txt", "roland", 20)
    assert memory.trash_entry(trash_id)["original_path"] == "folder/a.txt"
    assert len(memory.trash_entries(deleted_before=201)) == 1
    assert memory.trash_entries(deleted_before=200) == []
    assert not (tmp_path / "folder").exists() and not (tmp_path / ".trash").exists()
    assert memory.delete_trash_record(trash_id)
    assert memory.delete_file_record("folder/a.txt")
    assert memory.files() == []


@pytest.mark.parametrize("path", ["../a", "a/../b", "/etc/passwd", "a\\b", "", ".", "a\x00b"])
def test_metadata_paths_refuse_traversal(memory, path):
    with pytest.raises(ValueError):
        memory.record_file(path, 10)
    assert memory.files() == []


def test_signin_lifecycle_does_not_accept_url_credentials(memory):
    run_id = memory.add_agent_run("chat")
    with pytest.raises(ValueError, match="without credentials"):
        memory.add_signin_request(run_id, "https://user:password@example.test/login", 1000)
    identifier = memory.add_signin_request(run_id, "https://example.test/login", 1000, reason="x" * 400)
    assert memory.signin_request(identifier)["site"] == "example.test"
    assert len(memory.signin_request(identifier)["reason"]) == 300
    assert memory.set_signin_status(identifier, "in_progress")
    assert not memory.set_signin_status(identifier, "in_progress")
    assert memory.set_signin_status(identifier, "done", expected_status="in_progress")
    assert memory.signin_request(identifier)["finished"] is not None
    assert memory.expire_signin_requests(2000) == 0


def test_pending_and_active_signins_expire(memory):
    run_id = memory.add_agent_run("chat")
    a = memory.add_signin_request(run_id, "https://a.test/", 500)
    b = memory.add_signin_request(run_id, "https://b.test/", 500)
    assert memory.set_signin_status(b, "in_progress")
    assert memory.expire_signin_requests(500) == 2
    assert memory.signin_request(a)["status"] == memory.signin_request(b)["status"] == "expired"


def test_screen_records_are_bound_to_login_session_hash(memory):
    digest = hashlib.sha256(b"synthetic-session-token").hexdigest()
    with pytest.raises(ValueError, match="never a raw token"):
        memory.add_screen_session("synthetic-session-token")
    identifier = memory.add_screen_session(digest, "watch")
    assert not memory.touch_screen_session(identifier, "wrong-hash")
    assert not memory.end_screen_session(identifier, "wrong-hash")
    assert memory.touch_screen_session(identifier, digest)
    assert memory.end_screen_session(identifier, digest)
    assert not memory.touch_screen_session(identifier, digest)
    assert memory.screen_sessions() == []
    assert len(memory.screen_sessions(active_only=False)) == 1


def test_approval_argument_storage_limit_is_measured_in_bytes(memory):
    run_id = memory.add_agent_run("chat")
    with pytest.raises(ValueError, match="storage limit"):
        memory.add_approval(run_id, "tool", {"x": "é" * 32768}, "other", "Fixture", {}, 1000)
    assert memory.approvals() == []


def test_sql_parameters_cannot_change_schema(memory):
    chat_id = memory.new_chat()
    text = "'); DROP TABLE chats; --"
    memory.add_event(chat_id, "note", text, {"text": text})
    assert memory.chat_exists(chat_id)
    assert json.loads(memory._all("SELECT meta FROM messages")[0][0]) == {"text": text}
