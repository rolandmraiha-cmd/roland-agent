"""M3 confirmation gate: A3.1–A3.2 acceptance tests."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace

import pytest
from conftest import FakeBrain, call, make_config
from fastapi.testclient import TestClient

from agent.core import Agent
from agent.gate import POLICIES, Decision, Gate, Risk, RunState
from agent.memory import Memory
from agent.tools import TOOLS, ToolContext, call_tool
from agent.web.app import create_app

PASSWORD = "correct horse battery staple"


def ctx(memory, tmp_path, **kw):
    base = ToolContext(memory, tmp_path, "Europe/Helsinki", allow_shell=False)
    for key, value in kw.items():
        setattr(base, key, value)
    return base


def test_every_tool_has_a_policy():
    assert set(TOOLS) == set(POLICIES)


@pytest.mark.asyncio
async def test_unknown_tool_refused(tmp_path):
    memory = Memory(tmp_path / "agent.db")
    out = await call_tool(ctx(memory, tmp_path), "nope", {})
    assert out == "Error: there is no tool called nope."


@pytest.mark.asyncio
async def test_classifier_exception_is_forbidden(tmp_path, monkeypatch):
    memory = Memory(tmp_path / "agent.db")

    async def boom(c, a):
        raise RuntimeError("classifier exploded")

    monkeypatch.setitem(POLICIES, "list_files", replace(POLICIES["list_files"], classify=boom))
    out = await call_tool(ctx(memory, tmp_path), "list_files", {})
    assert out.startswith("Error: list_files isn't allowed: classifier error")


@pytest.mark.asyncio
async def test_no_gate_means_not_approved(tmp_path):
    memory = Memory(tmp_path / "agent.db")
    memory.remember("tea")
    out = await call_tool(ctx(memory, tmp_path), "forget", {"fact_id": 1})
    assert out.startswith("Not done:")
    assert "approvals aren't available" in out
    assert memory.facts()


def _login(client):
    origin = {"Origin": "http://testserver"}
    r = client.post("/login", data={"password": PASSWORD}, headers=origin, follow_redirects=False)
    assert r.status_code == 303, r.text
    client.headers["X-CSRF-Token"] = client.get("/api/status").json()["csrf"]
    client.headers["Origin"] = "http://testserver"


def _agent_with_script(tmp_path, script, **kw):
    config = make_config(tmp_path, **kw)
    return Agent(config, Memory(config.db_path), FakeBrain(script))


@pytest.mark.asyncio
async def test_gated_waits_and_runs_after_approval(tmp_path):
    agent = _agent_with_script(
        tmp_path,
        [
            ("", [call("forget", json.dumps({"fact_id": 1}))]),
            "Forgotten as you asked.",
        ],
    )
    agent.memory.remember("keep me")
    chat_id = agent.memory.new_chat()

    async def approve_soon():
        for _ in range(100):
            pending = agent.memory.approvals(status="pending")
            if pending:
                row = pending[0]
                await agent.gate.approve(row["id"], row["args_hash"], confirm=True)
                return
            await asyncio.sleep(0.05)
        raise AssertionError("no pending approval")

    async def run_chat():
        return [event async for event in agent.chat(chat_id, "please forget fact 1")]

    events, _ = await asyncio.gather(run_chat(), approve_soon())
    assert any(e.get("type") == "approval_required" for e in events)
    assert agent.memory.facts() == []
    assert any(r["status"] == "executed" for r in agent.memory.approvals(status="all"))


@pytest.mark.asyncio
async def test_rejected_returns_not_done_with_note(tmp_path):
    agent = _agent_with_script(
        tmp_path,
        [("", [call("forget", json.dumps({"fact_id": 1}))]), "ok"],
    )
    agent.memory.remember("stay")
    chat_id = agent.memory.new_chat()

    async def reject_soon():
        for _ in range(50):
            pending = agent.memory.approvals(status="pending")
            if pending:
                row = pending[0]
                await agent.gate.reject(row["id"], note="keep it", args_hash=row["args_hash"])
                return
            await asyncio.sleep(0.05)
        raise AssertionError("no pending")

    async def run_chat():
        return [e async for e in agent.chat(chat_id, "forget it")]

    events, _ = await asyncio.gather(run_chat(), reject_soon())
    tool_msgs = [m for turn in agent.brain.seen for m in turn if m.get("role") == "tool"]
    assert any("Not done: Roland rejected this." in (m.get("content") or "") for m in tool_msgs)
    assert any("Roland's note: keep it" in (m.get("content") or "") for m in tool_msgs)
    assert agent.memory.facts()


@pytest.mark.asyncio
async def test_expired_is_not_run(tmp_path, monkeypatch):
    agent = _agent_with_script(
        tmp_path,
        [("", [call("forget", json.dumps({"fact_id": 1}))]), "ok"],
        approval_timeout_min=1,
    )
    agent.memory.remember("stay")
    chat_id = agent.memory.new_chat()
    # Force immediate expiry by shrinking expires after insert.
    real_add = agent.memory.add_approval

    def add_expired(*args, **kwargs):
        kwargs = dict(kwargs)
        # expires positional is args[6] after run_id,tool,args,category,summary,details
        aid = real_add(*args, **kwargs)
        agent.memory._exec(
            "UPDATE approvals SET expires = ? WHERE id = ?",
            (time.time() - 1, aid),
        )
        return aid

    monkeypatch.setattr(agent.memory, "add_approval", add_expired)

    async def expire_loop():
        for _ in range(40):
            agent.gate.expire_due(time.time())
            await asyncio.sleep(0.05)

    events, _ = await asyncio.gather(
        asyncio.create_task(asyncio.wait_for(
            _collect(agent.chat(chat_id, "forget")), timeout=5
        )),
        expire_loop(),
    )
    assert agent.memory.facts()
    assert any(r["status"] == "expired" for r in agent.memory.approvals(status="all"))


async def _collect(aiter):
    return [e async for e in aiter]


@pytest.mark.asyncio
async def test_args_hash_mismatch_409(tmp_path):
    agent = _agent_with_script(tmp_path, [])
    app = create_app(agent, run_scheduler=False)
    with TestClient(app) as client:
        _login(client)
        run_id = agent.memory.add_agent_run("chat")
        aid = agent.memory.add_approval(
            run_id, "forget", {"fact_id": 1}, "delete", "Forget fact 1", {"fact_id": 1},
            time.time() + 60, needs_confirm=True,
        )
        res = client.post(
            f"/api/approvals/{aid}/approve",
            json={"args_hash": "0" * 64, "confirm": True},
        )
        assert res.status_code == 409
        assert agent.memory.approval(aid)["status"] == "pending"


@pytest.mark.asyncio
async def test_double_approve_409(tmp_path):
    agent = _agent_with_script(tmp_path, [])
    app = create_app(agent, run_scheduler=False)
    with TestClient(app) as client:
        _login(client)
        run_id = agent.memory.add_agent_run("chat")
        aid = agent.memory.add_approval(
            run_id, "forget", {"fact_id": 1}, "delete", "Forget fact 1", {"fact_id": 1},
            time.time() + 60, needs_confirm=True,
        )
        row = agent.memory.approval(aid)
        first = client.post(
            f"/api/approvals/{aid}/approve",
            json={"args_hash": row["args_hash"], "confirm": True},
        )
        assert first.status_code == 200
        second = client.post(
            f"/api/approvals/{aid}/approve",
            json={"args_hash": row["args_hash"], "confirm": True},
        )
        assert second.status_code == 409


@pytest.mark.asyncio
async def test_stored_args_are_used_not_new_ones(tmp_path):
    memory = Memory(tmp_path / "agent.db")
    memory.remember("a")
    memory.remember("b")
    config = make_config(tmp_path)
    from agent.audit import Audit

    audit = Audit(memory)
    gate = Gate(memory, audit, config)
    run = RunState(run_id=memory.add_agent_run("chat", chat_id=memory.new_chat()), chat_id=1)
    gate.register_run(run)
    context = ctx(memory, tmp_path, gate=gate, run=run, audit=audit, config=config)

    async def approve():
        for _ in range(50):
            pending = memory.approvals(status="pending")
            if pending:
                # Attacker supplies different args_hash / would want fact 2; we approve stored fact_id=1.
                await gate.approve(pending[0]["id"], pending[0]["args_hash"], confirm=True)
                return
            await asyncio.sleep(0.05)

    result_task = asyncio.create_task(call_tool(context, "forget", {"fact_id": 1}))
    await approve()
    result = await result_task
    assert "Forgotten" in result
    facts = dict(memory.facts())
    assert 1 not in facts and 2 in facts


@pytest.mark.asyncio
async def test_needs_confirm_requires_confirm_true(tmp_path):
    agent = _agent_with_script(tmp_path, [])
    app = create_app(agent, run_scheduler=False)
    with TestClient(app) as client:
        _login(client)
        run_id = agent.memory.add_agent_run("chat")
        aid = agent.memory.add_approval(
            run_id, "forget", {"fact_id": 1}, "delete", "Forget", {}, time.time() + 60,
        )
        row = agent.memory.approval(aid)
        assert row["needs_confirm"] is True
        res = client.post(
            f"/api/approvals/{aid}/approve",
            json={"args_hash": row["args_hash"], "confirm": False},
        )
        assert res.status_code == 400
        assert agent.memory.approval(aid)["status"] == "pending"
        ok = client.post(
            f"/api/approvals/{aid}/approve",
            json={"args_hash": row["args_hash"], "confirm": True},
        )
        assert ok.status_code == 200


@pytest.mark.asyncio
async def test_max_pending_approvals(tmp_path):
    config = make_config(tmp_path, max_pending_approvals=1)
    memory = Memory(config.db_path)
    from agent.audit import Audit

    audit = Audit(memory)
    gate = Gate(memory, audit, config)
    run1 = RunState(run_id=memory.add_agent_run("chat"), chat_id=1)
    run2 = RunState(run_id=memory.add_agent_run("chat"), chat_id=1)
    gate.register_run(run1)
    gate.register_run(run2)
    memory.add_approval(
        run1.run_id, "forget", {"fact_id": 1}, "delete", "Forget", {}, time.time() + 60,
    )
    outcome = await gate.request(
        ctx(memory, tmp_path, gate=gate, run=run2, config=config),
        "forget",
        {"fact_id": 2},
        Decision(Risk.GATED, "delete"),
    )
    assert not outcome.approved
    assert "too many pending" in outcome.message


@pytest.mark.asyncio
async def test_taint_set_after_fetch_url(tmp_path, monkeypatch):
    agent = _agent_with_script(
        tmp_path,
        [("", [call("fetch_url", json.dumps({"url": "https://example.com"}))]), "done"],
    )

    async def fake_fetch(ctx, args):
        return "HTTP 200 ok\n\nhello"

    monkeypatch.setitem(TOOLS, "fetch_url", (TOOLS["fetch_url"][0], fake_fetch))
    chat_id = agent.memory.new_chat()
    await _collect(agent.chat(chat_id, "fetch"))
    runs = [r for r in agent.memory._all("SELECT * FROM runs") ]
    assert runs and bool(runs[0]["tainted"]) is True


@pytest.mark.asyncio
async def test_remember_gated_when_tainted(tmp_path):
    memory = Memory(tmp_path / "agent.db")
    run = RunState(run_id=memory.add_agent_run("chat"), tainted=True)
    decision = await POLICIES["remember"].classify(ctx(memory, tmp_path, run=run), {"fact": "poison"})
    assert decision.risk is Risk.GATED
    # Without an approver the gated call cannot run.
    out = await call_tool(ctx(memory, tmp_path, run=run), "remember", {"fact": "poison"})
    assert out.startswith("Not done:")
    assert memory.facts() == []


@pytest.mark.asyncio
async def test_overwrite_of_roland_file_gated(tmp_path):
    memory = Memory(tmp_path / "agent.db")
    path = tmp_path / "notes.txt"
    path.write_text("roland")
    memory.record_file("notes.txt", path.stat().st_size, origin="upload", chat_id=None)
    run = RunState(run_id="r1", chat_id=1)
    decision = await POLICIES["write_file"].classify(ctx(memory, tmp_path, run=run), {"path": "notes.txt", "content": "x"})
    assert decision.risk is Risk.GATED


@pytest.mark.asyncio
async def test_overwrite_of_own_file_in_same_chat_safe(tmp_path):
    memory = Memory(tmp_path / "agent.db")
    path = tmp_path / "notes.txt"
    path.write_text("agent")
    memory.record_file("notes.txt", path.stat().st_size, origin="agent", chat_id=7)
    run = RunState(run_id="r1", chat_id=7, tainted=False)
    decision = await POLICIES["write_file"].classify(
        ctx(memory, tmp_path, run=run), {"path": "notes.txt", "content": "x"},
    )
    assert decision.risk is Risk.SAFE


@pytest.mark.asyncio
async def test_cancel_own_unapproved_job_safe_but_approved_job_gated(tmp_path):
    memory = Memory(tmp_path / "agent.db")
    from agent.schedule import next_run_after

    nxt = next_run_after("0 7 * * *", "Europe/Helsinki")
    unapproved = memory.add_job("idea", "0 7 * * *", "do", nxt, approved=False, origin="agent")
    approved = memory.add_job("ok", "0 7 * * *", "do", nxt, approved=True, origin="panel")
    context = ctx(memory, tmp_path)
    d1 = await POLICIES["cancel_job"].classify(context, {"job_id": unapproved})
    d2 = await POLICIES["cancel_job"].classify(context, {"job_id": approved})
    assert d1.risk is Risk.SAFE
    assert d2.risk is Risk.GATED


@pytest.mark.asyncio
async def test_model_cannot_approve_via_chat_text(tmp_path):
    agent = _agent_with_script(tmp_path, ["I cannot approve actions via plain text."])
    agent.memory.remember("keep")
    chat_id = agent.memory.new_chat()
    run_id = agent.memory.add_agent_run("chat", chat_id=chat_id)
    aid = agent.memory.add_approval(
        run_id, "forget", {"fact_id": 1}, "delete", "Forget", {}, time.time() + 120,
        chat_id=chat_id, needs_confirm=True,
    )
    # User message that looks like approval must not change the pending row.
    events = [e async for e in agent.chat(chat_id, "yes approve")]
    assert agent.memory.approval(aid)["status"] == "pending"
    assert agent.memory.facts()
    assert any(e.get("type") == "done" for e in events)


@pytest.mark.asyncio
async def test_job_gated_action_waits_then_expires(tmp_path, monkeypatch):
    agent = _agent_with_script(
        tmp_path,
        [("", [call("forget", json.dumps({"fact_id": 1}))]), "done"],
        job_approval_timeout_min=1,
    )
    agent.memory.remember("job-fact")
    from agent.schedule import next_run_after

    job_id = agent.memory.add_job(
        "clean", "0 7 * * *", "forget fact", next_run_after("0 7 * * *", "Europe/Helsinki"),
        approved=True, origin="panel",
    )
    job = agent.memory.job(job_id)
    real_add = agent.memory.add_approval

    def add_expired(*args, **kwargs):
        aid = real_add(*args, **kwargs)
        agent.memory._exec("UPDATE approvals SET expires = ? WHERE id = ?", (time.time() - 1, aid))
        return aid

    monkeypatch.setattr(agent.memory, "add_approval", add_expired)

    async def expire_loop():
        for _ in range(40):
            agent.gate.expire_due(time.time())
            await asyncio.sleep(0.05)

    (ok, out), _ = await asyncio.gather(
        asyncio.wait_for(agent.run_job(job), timeout=5),
        expire_loop(),
    )
    assert agent.memory.facts()
    assert any(r["status"] == "expired" for r in agent.memory.approvals(status="all"))


@pytest.mark.asyncio
async def test_audit_records_every_gate_decision(tmp_path, monkeypatch):
    agent = _agent_with_script(
        tmp_path,
        [
            ("", [call("list_files", "{}"), call("remember", json.dumps({"fact": "hi"}))]),
            "done",
        ],
    )
    chat_id = agent.memory.new_chat()
    await _collect(agent.chat(chat_id, "list and remember"))
    rows = agent.memory.audit_rows(limit=100)
    decisions = [r for r in rows if r["event"] == "gate_decision"]
    results = [r for r in rows if r["event"] == "tool_result"]
    assert {r["tool"] for r in decisions} >= {"list_files", "remember"}
    assert {r["decision"] for r in decisions if r["tool"] == "list_files"} == {"safe"}
    assert len(results) >= 2


def test_agent_wires_real_gate(make_agent):
    agent = make_agent()
    assert isinstance(agent.gate, Gate)
    assert agent.ctx.gate is agent.gate
