import json
import time

import pytest

from agent.scheduler import run_due_jobs
from tests.conftest import call


async def collect(gen):
    return [e async for e in gen]


@pytest.mark.asyncio
async def test_chat_streams_and_saves(make_agent):
    agent = make_agent(["Hello there Roland"])
    chat = agent.memory.new_chat()
    events = await collect(agent.chat(chat, "hi"))
    assert "".join(e["text"] for e in events if e["type"] == "text").strip() == "Hello there Roland"
    assert events[-1]["type"] == "done"
    msgs = agent.memory.messages(chat)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert agent.memory.chats()[0]["title"] == "hi"


@pytest.mark.asyncio
async def test_history_and_facts_reach_model(make_agent):
    agent = make_agent(["one", "two"])
    agent.memory.remember("Roland likes short answers")
    chat = agent.memory.new_chat()
    await collect(agent.chat(chat, "first"))
    await collect(agent.chat(chat, "second"))
    sent = agent.brain.seen[-1]
    assert "Roland likes short answers" in sent[0]["content"]
    assert [m["content"] for m in sent[1:]] == ["first", "one", "second"]


@pytest.mark.asyncio
async def test_tool_loop_writes_file(make_agent):
    agent = make_agent([
        ("", [call("write_file", json.dumps({"path": "notes/a.txt", "content": "hey"}))]),
        "Saved it.",
    ])
    chat = agent.memory.new_chat()
    events = await collect(agent.chat(chat, "save hey"))
    assert any(e["type"] == "tool" and "write_file" in e["text"] for e in events)
    assert (agent.config.workspace / "notes/a.txt").read_text() == "hey"
    tool_msg = agent.brain.seen[1][-1]
    assert tool_msg["role"] == "tool" and "Saved" in tool_msg["content"]


@pytest.mark.asyncio
async def test_daily_cap(make_agent):
    agent = make_agent(["a", "b", "c"], daily_call_limit=2)
    chat = agent.memory.new_chat()
    await collect(agent.chat(chat, "1"))
    await collect(agent.chat(chat, "2"))
    events = await collect(agent.chat(chat, "3"))
    assert events[-1]["type"] == "error" and "daily limit" in events[-1]["message"]
    assert agent.calls_left() == 0


@pytest.mark.asyncio
async def test_max_tool_steps(make_agent):
    loop = [("", [call("list_files")])] * 10
    agent = make_agent(loop, max_tool_steps=2)
    events = await collect(agent.chat(agent.memory.new_chat(), "go"))
    assert events[-1]["type"] == "error" and "tool steps" in events[-1]["message"]


@pytest.mark.asyncio
async def test_job_runs_and_saves_result(make_agent):
    agent = make_agent([
        ("", [call("remember", json.dumps({"fact": "job ran"}))]),
        "Morning summary done",
    ])
    job_id = agent.memory.add_job("Morning", "0 7 * * *", "summarise", time.time() - 1)
    assert await run_due_jobs(agent) == 1
    run = agent.memory.runs()[0]
    assert run["ok"] == 1 and "Morning summary done" in run["output"] and "remember" in run["output"]
    assert agent.memory.job(job_id).next_run > time.time()
    assert await run_due_jobs(agent) == 0


@pytest.mark.asyncio
async def test_job_cannot_schedule_jobs(make_agent):
    agent = make_agent([
        ("", [call("schedule_job", json.dumps({"name": "x", "cron": "* * * * *", "prompt": "x"}))]),
        "done",
    ])
    agent.memory.add_job("J", "0 7 * * *", "p", time.time() - 1)
    await run_due_jobs(agent)
    assert len(agent.memory.jobs()) == 1
    assert all("schedule_job" not in names for names in agent.brain.tools)
    assert "isn't available" in agent.brain.seen[1][-1]["content"]
