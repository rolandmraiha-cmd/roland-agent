"""A8.2 capture proves actual context, durable privacy exclusions and label locks."""

import json

import pytest
from fastapi.testclient import TestClient

from agent.web.app import create_app
from tests.test_files_api import login


async def chat(agent, text="hello"):
    identifier = agent.memory.new_chat()
    events = [event async for event in agent.chat(identifier, text)]
    return identifier, events[-1]["id"]


@pytest.mark.asyncio
async def test_capture_off_stores_nothing(make_agent):
    agent = make_agent(["reply"])
    _, message = await chat(agent)
    result = agent.capture.feedback(message, 1)
    assert not result["captured"]
    assert not agent.memory._all("SELECT * FROM training_examples")
    assert not list((agent.capture.root / "pending").glob("*"))


@pytest.mark.asyncio
async def test_feedback_upsert_and_lock_after_dataset(make_agent):
    agent = make_agent(["reply"])
    agent.memory.set_meta("training_capture", "1")
    _, message = await chat(agent)
    agent.capture.feedback(message, 1)
    agent.capture.feedback(message, -1, "better")
    assert len(agent.memory._all("SELECT * FROM feedback")) == 1
    assert len(agent.memory._all("SELECT * FROM training_examples")) == 1
    row = agent.memory._all("SELECT * FROM training_examples")[0]
    assert row["source"] == "correction"
    assert json.loads(row["target_json"])["text"] == "better"
    agent.memory._exec("UPDATE training_examples SET used_in_dataset='dataset'")
    with pytest.raises(LookupError):
        agent.capture.feedback(message, 1)


@pytest.mark.asyncio
async def test_saved_feedback_says_whether_the_vote_was_captured(make_agent):
    agent = make_agent(["reply", "reply"])
    _, off = await chat(agent)
    assert agent.capture.saved_feedback(off) is None
    agent.capture.feedback(off, 1)
    saved = agent.capture.saved_feedback(off)
    assert saved["rating"] == 1 and saved["captured"] is False
    agent.memory.set_meta("training_capture", "1")
    _, on = await chat(agent)
    agent.capture.feedback(on, -1, "better")
    saved = agent.capture.saved_feedback(on)
    assert saved["captured"] is True and saved["correction"] == "better"
    assert agent.capture.saved_feedback(off)["captured"] is False
    # An approval label on the same reply is not this vote's example.
    agent.memory._exec("UPDATE training_examples SET source='approved_call' WHERE message_id=?", (on,))
    assert agent.capture.saved_feedback(on)["captured"] is False
    # Deleting the example in the review list leaves the vote, not the capture.
    agent.memory._exec("DELETE FROM training_examples WHERE message_id=?", (on,))
    saved = agent.capture.saved_feedback(on)
    assert saved["rating"] == -1 and saved["captured"] is False


def test_reloaded_chat_and_feedback_route_carry_captured(make_agent):
    agent = make_agent(["reply"])
    agent.memory.set_meta("training_capture", "1")
    with TestClient(create_app(agent, False), base_url="https://agent.test") as client:
        login(client)
        chat_id = client.post("/api/chats").json()["id"]
        with client.stream("POST", f"/api/chats/{chat_id}/send", json={"text": "hello"}) as response:
            "".join(response.iter_text())

        def reply():
            return client.get(f"/api/chats/{chat_id}/messages").json()["messages"][-1]

        message = reply()
        route = f"/api/messages/{message['id']}/feedback"
        assert message["role"] == "assistant" and message["feedback"] is None
        assert client.get(route).json() == {
            "message_id": message["id"], "rating": None, "used_in_dataset": None, "captured": False
        }
        assert client.post(route, json={"rating": 1}).json()["captured"] is True
        assert reply()["feedback"]["rating"] == 1 and reply()["feedback"]["captured"] is True
        assert client.get(route).json()["captured"] is True
        assert client.delete(route).status_code == 200
        assert reply()["feedback"] is None and client.get(route).json()["captured"] is False


@pytest.mark.asyncio
async def test_never_chat_excluded(make_agent):
    agent = make_agent(["reply"])
    agent.memory.set_meta("training_capture", "1")
    identifier = agent.memory.new_chat()
    agent.memory._exec("UPDATE chats SET training_mode='never' WHERE id=?", (identifier,))
    events = [event async for event in agent.chat(identifier, "hello")]
    assert not agent.capture.feedback(events[-1]["id"], 1)["captured"]


@pytest.mark.asyncio
async def test_signin_content_never_captured(make_agent):
    agent = make_agent(["reply"])
    agent.memory.set_meta("training_capture", "1")
    agent.memory.add_signin_request("fake-run", "https://example.com/login", 9999999999)
    _, message = await chat(agent, "a secret sign-in turn")
    assert not agent.capture.feedback(message, 1)["captured"]
    assert not agent.memory._all("SELECT * FROM training_examples")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["watch", "control"])
async def test_every_active_screen_session_blocks_capture(make_agent, mode):
    agent = make_agent(["reply"])
    agent.memory.set_meta("training_capture", "1")
    agent.memory.add_screen_session("a" * 64, mode)
    _, message = await chat(agent)
    assert not agent.capture.feedback(message, 1)["captured"]


def context(agent, tainted=False):
    from agent.gate import RunState

    run = RunState("test-run", agent.memory.new_chat(), tainted=tainted)
    agent.memory.set_meta("training_capture", "1")
    action = {"action": "tool", "tool": "delete_file", "args": {"path": "notes/a.txt"}}
    agent.capture.record(run, [{"role": "system", "content": "actual budgeted context"}], [], action)
    return run, action


def test_tainted_examples_default_excluded(make_agent):
    agent = make_agent()
    run, _ = context(agent, True)
    agent.capture.persist(agent.capture.pending[run.run_id][-1], "thumbs_up")
    assert agent.memory._all("SELECT include FROM training_examples")[0][0] == 0


def test_gate_reject_with_alternative_creates_pair(make_agent):
    agent = make_agent()
    run, _ = context(agent)
    agent.capture.gate_label(
        {"chat_id": run.chat_id, "run_id": run.run_id, "id": "approval", "status": "rejected"},
        {"action": "reply", "text": "Keep the file"},
    )
    assert len(agent.memory._all("SELECT * FROM preference_pairs")) == 1


def test_gate_reject_without_alternative_creates_no_pair(make_agent):
    agent = make_agent()
    run, _ = context(agent)
    agent.capture.gate_label(
        {"chat_id": run.chat_id, "run_id": run.run_id, "id": "approval", "status": "rejected"}
    )
    assert not agent.memory._all("SELECT * FROM preference_pairs")


def test_approved_call_is_positive_example(make_agent):
    agent = make_agent()
    run, action = context(agent)
    agent.capture.gate_label(
        {"chat_id": run.chat_id, "run_id": run.run_id, "id": "approval", "status": "approved"}
    )
    row = agent.memory._all("SELECT * FROM training_examples")[0]
    assert row["source"] == "approved_call" and json.loads(row["target_json"]) == action


def test_chat_delete_cascades(make_agent):
    agent = make_agent()
    run, _ = context(agent)
    agent.capture.gate_label(
        {"chat_id": run.chat_id, "run_id": run.run_id, "id": "approval", "status": "rejected"},
        {"action": "reply", "text": "Keep it"},
    )
    agent.memory.delete_chat(run.chat_id)
    assert not agent.memory._all("SELECT * FROM training_examples")
    assert not agent.memory._all("SELECT * FROM preference_pairs")


def test_invalid_correction_cannot_execute_or_save(make_agent):
    agent = make_agent()
    chat_id = agent.memory.new_chat()
    message = agent.memory._add_message(chat_id, "assistant", "reply", "text", None, None)
    with pytest.raises(ValueError):
        agent.capture.feedback(
            message, -1, correction_action={"action": "tool", "tool": "delete_file", "args": {"path": 42}}
        )
    assert not agent.memory._all("SELECT * FROM feedback")
