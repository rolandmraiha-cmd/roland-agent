"""A7.2: the sign-in flow (§6.7). The agent asks, Roland signs in himself on the screen, and
only his Done button ends the wait. A fake browserd stands in for the browser."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace

import pytest
from conftest import call
from screen_helpers import PNG, VNC_FULL, ScreenBrowserd, screen_agent, web

from agent.core import SIGNIN_ASK, SIGNIN_TELL
from agent.gate import POLICIES, RunState
from agent.models.context import SIGNIN_HINT, action_prompt, estimate_tokens
from agent.screen import ScreenError
from agent.signin import DONE, DONE_LOOK, DONE_PAGE, NEEDS_APPROVAL, ONLY_IN_CHAT
from agent.tools import MAX_FACTS, TOOLS, call_tool, schemas
from agent.tools_browser import (
    BROWSER_TOOLS,
    SCREEN_TOOLS,
    SIGNIN_FORM_ASK,
    SIGNIN_FORM_STILL,
    SIGNIN_FORM_TELL,
)

LOGIN = "https://shop.example/login"
LOCKED = "Error: Roland is using the browser right now."


@pytest.fixture
def fake():
    return ScreenBrowserd()


def ask(url: str = LOGIN, **extra):
    return call("request_signin", json.dumps({"url": url, "site": "shop.example", "reason": "to read your orders", **extra}))


async def waiting(agent, timeout: float = 5.0) -> dict:
    """The sign-in the agent is waiting on, once it shows up."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = agent.memory.active_signin_requests()
        if rows:
            return rows[0]
        await asyncio.sleep(0.01)
    raise AssertionError("no sign-in was requested")


async def chat(agent, text: str = "check my orders"):
    chat_id = agent.memory.new_chat()
    events: list[dict] = []

    async def run():
        async for event in agent.chat(chat_id, text):
            events.append(event)
        return events

    return chat_id, events, asyncio.create_task(run())


def tool_results(agent) -> list[str]:
    seen: list[str] = []
    for turn in agent.brain.seen:
        for message in turn:
            if message.get("role") == "tool" and message["content"] not in seen:
                seen.append(message["content"])
    return seen


async def test_request_signin_sets_user_mode_and_waits(tmp_path, fake):
    agent = screen_agent(tmp_path, fake, [("", [ask()]), "Cancelled then."])
    chat_id, events, task = await chat(agent)
    row = await waiting(agent)
    await asyncio.sleep(0.5)
    assert not task.done() and len(agent.brain.seen) == 1  # the model hears nothing until Roland acts
    # The page was opened, then the browser handed to Roland, in that order.
    actions = [(path, body) for _, path, body in fake.calls if path in {"/v1/navigate", "/v1/user-mode"}]
    assert actions == [("/v1/navigate", {"url": LOGIN, "new_tab": False}), ("/v1/user-mode", {"on": True})]
    assert fake.mode == "user" and agent.screens.roland_has_browser()
    assert row["status"] == "pending" and row["site"] == "shop.example" and row["url"] == LOGIN
    assert row["chat_id"] == chat_id and row["reason"] == "to read your orders"
    assert 29 * 60 < row["expires"] - time.time() <= 30 * 60
    card = next(event for event in events if event["type"] == "signin_required")["signin"]
    assert card == {
        "id": row["id"], "site": "shop.example", "url": LOGIN, "reason": "to read your orders",
        "status": "pending", "created": row["created"], "expires": row["expires"], "chat_id": chat_id,
    }
    requested = agent.memory.audit_rows(event="signin_requested")[0]
    assert requested["actor"] == "agent" and requested["detail"] == {"signin_id": row["id"], "site": "shop.example"}
    timeline = [item for item in agent.memory.timeline(chat_id) if item["kind"] == "signin"]
    assert timeline[0]["meta"] == {"signin_id": row["id"], "status": "pending"}
    # Only one sign-in can wait at a time: there is one browser to hand over.
    assert await agent.signins.request(agent.ctx, LOGIN) == ONLY_IN_CHAT
    other = RunState(run_id="other", chat_id=agent.memory.new_chat(), origin="chat")
    assert "another sign-in is already waiting" in await agent.signins.request(replace(agent.ctx, run=other), LOGIN)
    await agent.signins.cancel(row["id"])
    assert (await asyncio.wait_for(task, 5))[-1]["reply"].strip() == "Cancelled then."


async def test_done_resolves_and_unlocks(tmp_path, fake):
    agent = screen_agent(tmp_path, fake, [("", [ask()]), ("", [call("browser_snapshot")]), "You have 2 orders."])
    _, events, task = await chat(agent)
    row = await waiting(agent)
    # Roland opens the sign-in screen: full control, tied to this sign-in.
    session = await agent.screens.start("c" * 64, "control", row["id"])
    assert agent.memory.signin_request(row["id"])["status"] == "in_progress"
    assert agent.screens.password_for(session["mode"]) == VNC_FULL and session["signin_id"] == row["id"]
    await asyncio.sleep(0.3)
    assert not task.done()
    before = fake.disconnects
    assert await agent.signins.done(row["id"]) == {"status": "done"}
    events = await asyncio.wait_for(task, 5)
    assert events[-1]["type"] == "done" and events[-1]["reply"].strip() == "You have 2 orders."
    results = tool_results(agent)
    # The button is not taken as proof: the model gets the page as it is now, and here the
    # sign-in form is still on it. It is not told to ask again.
    assert DONE.format(site="shop.example") + DONE_PAGE + SIGNIN_FORM_STILL in results[0]
    assert '[e1] textbox "Password" (sensitive, value hidden)' in results[0]
    assert SIGNIN_FORM_TELL in results[0] and SIGNIN_FORM_ASK not in results[0]
    # The agent has the browser again and its snapshot went through; no password value in it.
    assert fake.mode == "agent" and not agent.screens.roland_has_browser()
    assert '[e1] textbox "Password" (sensitive, value hidden)' in results[1]
    assert agent.memory.signin_request(row["id"])["status"] == "done"
    assert agent.memory.screen_sessions() == [] and fake.disconnects == before + 1
    # Roland's screen was cut and the browser handed back before the model was told.
    order = [path for path in fake.paths("/v1/") if path in {"/v1/vnc/disconnect", "/v1/user-mode", "/v1/snapshot"}]
    # (Two snapshots: the page handed over with the news, then the one the model asked for.)
    assert order[-4:] == ["/v1/vnc/disconnect", "/v1/user-mode", "/v1/snapshot", "/v1/snapshot"]
    assert [(e["type"], e.get("status")) for e in events if e["type"].startswith("signin_")] == [
        ("signin_required", None), ("signin_resolved", "done"),
    ]
    resolved = agent.memory.audit_rows(event="signin_resolved")[0]
    assert resolved["actor"] == "roland" and resolved["decision"] == "done"
    assert resolved["detail"] == {"signin_id": row["id"], "site": "shop.example"}
    end = agent.memory.audit_rows(event="screen_session_end")[0]["detail"]
    assert end["mode"] == "control" and end["reason"] == "signin_done"
    # A finished sign-in can't be finished, cancelled or reopened again.
    for again in (agent.signins.done, agent.signins.cancel):
        with pytest.raises(LookupError):
            await again(row["id"])
    with pytest.raises(KeyError):
        await agent.signins.done("no-such-id")
    with pytest.raises(ScreenError, match="no longer waiting"):
        await agent.screens.start("c" * 64, "control", row["id"])


@pytest.mark.parametrize("button", ["done", "cancel"])
async def test_the_model_is_not_told_before_the_browser_is_back(tmp_path, fake, button):
    """Cutting Roland's screen takes a moment with a real browser (x11vnc is restarted).
    The tool must not return in that moment: the model's next call would still be refused.
    Found by the live sign-in test, where the snapshot after "I'm done" came back locked."""
    fake.disconnect_delay = 0.6  # longer than the tool's own polling
    agent = screen_agent(tmp_path, fake, [("", [ask()]), ("", [call("browser_snapshot")]), "Read it."])
    _, _events, task = await chat(agent)
    row = await waiting(agent)
    await agent.screens.start("c" * 64, "control", row["id"])
    decide = agent.signins.done if button == "done" else agent.signins.cancel
    await decide(row["id"])
    await asyncio.wait_for(task, 5)
    results = tool_results(agent)
    assert LOCKED not in results[1] and "URL: https://shop.example/login" in results[1]
    order = [path for path in fake.paths("/v1/") if path in {"/v1/vnc/disconnect", "/v1/user-mode", "/v1/snapshot"}]
    # After "I'm done" the page is read once for the news itself, then again for the model's own call.
    snapshots = ["/v1/snapshot"] * (2 if button == "done" else 1)
    assert order[-2 - len(snapshots):] == ["/v1/vnc/disconnect", "/v1/user-mode", *snapshots]
    # That first read found the browser unlocked as well.
    assert LOCKED not in results[0] and ("This is the page now" in results[0]) == (button == "done")


async def test_done_without_opening_the_screen_counts_too(tmp_path, fake):
    """He may already be signed in: pressing I'm done on the card is enough."""
    agent = screen_agent(tmp_path, fake, [("", [ask()]), "ok"])
    _, _, task = await chat(agent)
    row = await waiting(agent)
    assert await agent.signins.done(row["id"]) == {"status": "done"}
    await asyncio.wait_for(task, 5)
    assert agent.memory.signin_request(row["id"])["status"] == "done" and fake.mode == "agent"


async def test_cancel_and_timeout_messages(tmp_path, fake):
    agent = screen_agent(tmp_path, fake, [("", [ask()]), "a", ("", [ask()]), "b", ("", [ask()]), "c"])
    # Cancel
    _, events, task = await chat(agent)
    row = await waiting(agent)
    assert await agent.signins.cancel(row["id"]) == {"status": "cancelled"}
    await asyncio.wait_for(task, 5)
    assert "Roland cancelled the sign-in." in tool_results(agent)[-1]
    assert agent.memory.signin_request(row["id"])["status"] == "cancelled" and fake.mode == "agent"
    # Timeout
    _, events, task = await chat(agent)
    row = await waiting(agent)
    agent.memory._exec("UPDATE signin_requests SET expires = ? WHERE id = ?", (time.time() - 1, row["id"]))
    events = await asyncio.wait_for(task, 5)
    assert "Roland didn't finish signing in within 30 minutes." in tool_results(agent)[-1]
    assert agent.memory.signin_request(row["id"])["status"] == "expired" and fake.mode == "agent"
    assert ("signin_resolved", "expired") in [(e["type"], e.get("status")) for e in events]
    expired = agent.memory.audit_rows(event="signin_resolved")[0]
    assert expired["actor"] == "system" and expired["decision"] == "expired" and expired["detail"]["reason"] == "timeout"
    # Stop button
    chat_id, events, task = await chat(agent)
    row = await waiting(agent)
    assert await agent.stop_chat(chat_id)
    events = await asyncio.wait_for(task, 5)
    assert agent.memory.signin_request(row["id"])["status"] == "cancelled"
    assert fake.mode == "agent" and not agent.screens.roland_has_browser()
    assert events[-1]["type"] == "done" and events[-1]["reply"].endswith("[stopped by Roland]")
    assert ("signin_resolved", "cancelled") in [(e["type"], e.get("status")) for e in events]
    stopped = agent.memory.audit_rows(event="signin_resolved")[0]
    assert stopped["decision"] == "cancelled" and stopped["detail"]["reason"] == "stopped"
    # A restart cancels whatever was still waiting.
    signin_id = agent.memory.add_signin_request("r", LOGIN, time.time() + 600, chat_id=chat_id)
    assert agent.screens.roland_has_browser()
    assert agent.signins.reset_on_startup() == 1
    assert agent.memory.signin_request(signin_id)["status"] == "cancelled" and not agent.screens.roland_has_browser()


async def test_agent_browser_tools_locked_during_signin(tmp_path, fake):
    agent = screen_agent(tmp_path, fake, [("", [ask()]), "ok"])
    async with web(agent) as client:
        _, _, task = await chat(agent)
        row = await waiting(agent)
        fake.mode = "agent"  # even a browserd that forgot user mode is not asked
        before = len(fake.calls)
        assert await call_tool(agent.ctx, "browser_snapshot", {}) == LOCKED
        assert await call_tool(agent.ctx, "browser_screenshot", {}) == LOCKED
        assert await call_tool(agent.ctx, "browser_scroll", {"direction": "down"}) == LOCKED
        assert await call_tool(agent.ctx, "browser_open", {"url": "https://example.com/"}) == LOCKED
        for name in ("browser_click", "browser_type"):
            out = await call_tool(agent.ctx, name, {"ref": "e1", "text": "x"})
            assert out.startswith("Error:") and "Roland is using the browser right now" in out, out
        # Roland's own thumbnail on the Browser tab pauses too.
        assert (await client.post("/api/browser/screenshot")).status_code == 423
        assert len(fake.calls) == before  # none of it reached the browser
        assert not (agent.config.workspace / "screenshots").exists()
        status = (await client.get("/api/status")).json()
        assert status["pending_signins"] == 1 and status["screen"] == {"enabled": True}
        assert status["browser"]["mode"] == "agent"  # status itself stays readable
        assert (await client.post(f"/api/signin/{row['id']}/cancel")).json() == {"status": "cancelled"}
        await asyncio.wait_for(task, 5)
        assert (await agent.ctx.browser.screenshot()) == PNG


async def test_chat_text_done_does_not_resolve(tmp_path, fake):
    agent = screen_agent(tmp_path, fake, [("", [ask()]), "Signed in, thanks."])
    async with web(agent) as client:
        chat_id = (await client.post("/api/chats")).json()["id"]
        send = asyncio.create_task(client.post(f"/api/chats/{chat_id}/send", json={"text": "check my orders"}))
        row = await waiting(agent)
        for text in ("done", "I'm done", "yes", "ok I signed in"):
            refused = await client.post(f"/api/chats/{chat_id}/send", json={"text": text})
            assert refused.status_code == 409
        await asyncio.sleep(0.3)
        assert agent.memory.signin_request(row["id"])["status"] == "pending" and not send.done()
        assert len(agent.brain.seen) == 1 and fake.mode == "user"
        # Reloading the chat shows the card again, and the Browser tab lists it.
        history = (await client.get(f"/api/chats/{chat_id}/messages")).json()
        assert [item["id"] for item in history["pending_signins"]] == [row["id"]] and history["busy"] is True
        assert [item["id"] for item in (await client.get("/api/signin?status=pending")).json()] == [row["id"]]
        assert (await client.get("/api/signin?status=done")).status_code == 400
        other_chat = (await client.post("/api/chats")).json()["id"]
        assert (await client.get(f"/api/chats/{other_chat}/messages")).json()["pending_signins"] == []
        # Only the button counts.
        assert (await client.post("/api/signin/nope/done")).status_code == 404
        done = await client.post(f"/api/signin/{row['id']}/done")
        assert done.status_code == 200 and done.json() == {"status": "done"}
        stream = (await asyncio.wait_for(send, 5)).text
        assert '"type": "signin_required"' in stream and '"type": "signin_resolved"' in stream
        assert "Signed in, thanks." in stream
        assert (await client.post(f"/api/signin/{row['id']}/done")).status_code == 409
        assert (await client.post(f"/api/signin/{row['id']}/cancel")).status_code == 409
        assert (await client.get("/api/status")).json()["pending_signins"] == 0


async def finished(agent, fake) -> str:
    """Roland presses I'm done; returns what the model is told."""
    _, _, task = await chat(agent)
    row = await waiting(agent)
    assert await agent.signins.done(row["id"]) == {"status": "done"}
    await asyncio.wait_for(task, 5)
    return tool_results(agent)[0]


async def test_done_hands_over_the_signed_in_page(tmp_path, fake):
    """What the model answers from is the page, not the button: here it shows his account."""
    agent = screen_agent(tmp_path, fake, [("", [ask()]), "You are signed in."])
    fake.signed_in = True
    told = await finished(agent, fake)
    assert DONE.format(site="shop.example") + DONE_PAGE + f"URL: {LOGIN}" in told
    assert "Signed in as roland" in told and '[e1] link "Sign out"' in told
    assert SIGNIN_FORM_STILL not in told and "sign-in form" not in told
    # One snapshot, taken after the browser came back to the agent.
    assert fake.paths("/v1/snapshot") == ["/v1/snapshot"] and fake.mode == "agent"
    # Page content reached the model, so the run is marked as having seen untrusted data.
    assert [bool(row["tainted"]) for row in agent.memory._all("SELECT tainted FROM runs")] == [True]


async def test_done_without_a_readable_page_tells_the_model_to_look(tmp_path, fake):
    agent = screen_agent(tmp_path, fake, [("", [ask()]), "I couldn't read the page."])
    fake.down_paths = {"/v1/snapshot"}
    told = await finished(agent, fake)
    assert DONE.format(site="shop.example") + DONE_LOOK in told and DONE_PAGE not in told


async def test_the_page_after_done_fits_the_tool_output_limit(tmp_path, fake):
    """A long page is cut by the snapshot's own rule, with its note on how to read on, and the
    lines above it are not pushed out by it."""
    agent = screen_agent(tmp_path, fake, [("", [ask()]), "ok"], model_tool_output_chars=3000)
    fake.extra_links = 400
    told = await finished(agent, fake)
    assert len(told) <= 3000 and told.endswith("</tool_output>")
    assert SIGNIN_FORM_STILL in told and "more elements not shown" in told


async def test_cancel_and_timeout_hand_over_no_page(tmp_path, fake):
    agent = screen_agent(tmp_path, fake, [("", [ask()]), "ok"])
    _, _, task = await chat(agent)
    row = await waiting(agent)
    await agent.signins.cancel(row["id"])
    await asyncio.wait_for(task, 5)
    assert "Roland cancelled the sign-in." in tool_results(agent)[0]
    assert fake.paths("/v1/snapshot") == []


def test_the_action_prompt_says_what_to_do_with_a_sign_in_request():
    """The tool list has no descriptions. Without this line next to it the model answered
    "I can't assist with logging into websites" to "log in to this site" (2026-10-08)."""
    with_tool = action_prompt(schemas(set()))
    assert SIGNIN_HINT in with_tool and "do not refuse" in SIGNIN_HINT
    assert with_tool.index(SIGNIN_HINT) < with_tool.index("fetch_url(")
    assert SIGNIN_HINT not in action_prompt(schemas({"request_signin"}))
    assert "request_signin" not in action_prompt(schemas({"request_signin"}))
    assert "request_signin" not in action_prompt([])


async def test_request_signin_not_available_in_jobs(tmp_path, fake):
    # Its answer carries page content since the sign-in follow-up, so it taints like a snapshot.
    assert POLICIES["request_signin"].in_jobs is False and POLICIES["request_signin"].taints is True
    assert "request_signin" in TOOLS and SCREEN_TOOLS == {"request_signin"} and SCREEN_TOOLS <= BROWSER_TOOLS
    agent = screen_agent(tmp_path, fake, [("", [ask()]), "Needs sign-in to shop.example"])
    job_id = agent.memory.add_job("Orders", "0 7 * * *", "Check my orders at shop.example", 0)
    ok, output = await agent.run_job(agent.memory.job(job_id))
    assert ok and "Needs sign-in to shop.example" in output
    offered = set(agent.brain.tools[0])
    assert "request_signin" not in offered and "schedule_job" not in offered and "browser_open" in offered
    assert "Error: request_signin isn't available here." in tool_results(agent)[0]
    system = agent.brain.seen[0][0]["content"]
    assert 'answer "Needs sign-in to"' in system and "request_signin" not in system
    assert "stop and tell Roland" in system
    # Nothing was opened, handed over or recorded.
    assert agent.memory.active_signin_requests() == [] and fake.mode == "agent"
    assert fake.paths("/v1/navigate") == [] and fake.paths("/v1/user-mode") == []
    assert agent.memory.audit_rows(event="signin_requested") == []
    # Even called directly with a job's run, the tool refuses.
    job_run = RunState(run_id="job-run", job_id=job_id, origin="job")
    assert await call_tool(replace(agent.ctx, run=job_run), "request_signin", {"url": LOGIN}) == ONLY_IN_CHAT
    assert await call_tool(agent.ctx, "request_signin", {"url": LOGIN}) == ONLY_IN_CHAT
    # A job's snapshot of a sign-in page says to tell Roland, not to call a tool it doesn't have.
    assert SIGNIN_FORM_TELL in await call_tool(replace(agent.ctx, run=job_run), "browser_snapshot", {})


async def test_the_tool_is_offered_only_with_the_screen_on(tmp_path, fake):
    on = screen_agent(tmp_path / "on", fake, ["hi"], model_ctx=3072, model_max_new_tokens=768)
    for i in range(MAX_FACTS):
        on.memory.remember(f"Preference {i}: " + "x" * 180)
    [event async for event in on.chat(on.memory.new_chat(), "hello")]
    system = on.brain.seen[0][0]["content"]
    assert "request_signin" in on.brain.tools[0]
    assert "request_signin(url:string, site?:string, reason?:string)" in system
    assert SIGNIN_ASK in system and SIGNIN_HINT in system and SIGNIN_TELL not in system
    # Still inside the small model's budget with a full fact store.
    assert estimate_tokens(system) <= on.config.model_system_prompt_budget
    assert len(TOOLS["request_signin"][0]["function"]["description"]) <= 160
    assert SIGNIN_FORM_ASK in await call_tool(on.ctx, "browser_snapshot", {})

    for settings in (dict(screen_enabled=False), dict(browser_enabled=False)):
        off = screen_agent(tmp_path / str(sorted(settings)), fake, ["hi"], **settings)
        [event async for event in off.chat(off.memory.new_chat(), "hello")]
        assert "request_signin" not in off.brain.tools[0]
        assert "request_signin" not in off.brain.seen[0][0]["content"]
        out = await call_tool(off.ctx, "request_signin", {"url": LOGIN})
        assert out.startswith("Error:") and off.memory.active_signin_requests() == []
    off = screen_agent(tmp_path / "hint", fake, screen_enabled=False)
    assert SIGNIN_FORM_TELL in await call_tool(off.ctx, "browser_snapshot", {})


@pytest.mark.parametrize("url,expected", [
    ("", "give the address of the sign-in page"),
    ("ftp://shop.example/login", "give the address of the sign-in page"),
    ("javascript:alert(1)", "give the address of the sign-in page"),
    ("https://roland:hunter2@shop.example/login", "must not carry a user name or password"),
    ("https://" + "a" * 2050, "give the address of the sign-in page"),
    ("http://10.0.0.5/login", "the browser refused that address"),
])
async def test_addresses_that_cannot_be_sign_in_pages(tmp_path, fake, url, expected):
    agent = screen_agent(tmp_path, fake, [("", [ask(url)]), "ok"])
    _, events, task = await chat(agent)
    await asyncio.wait_for(task, 5)
    result = tool_results(agent)[0]
    assert "Error:" in result and expected in result
    assert agent.memory._all("SELECT * FROM signin_requests") == []
    assert fake.mode == "agent" and not any(e["type"].startswith("signin_") for e in events)
    assert "hunter2" not in str(agent.memory._all("SELECT detail FROM audit_log WHERE event != 'tool_result'"))


async def test_request_signin_is_no_way_around_browser_open_approval(tmp_path, fake):
    """An address browser_open would ask Roland about is not opened by request_signin either."""
    gated = "https://shop.example/account/delete/login"
    agent = screen_agent(tmp_path, fake, [("", [ask(gated)]), "ok", ("", [ask(gated)]), "ok"])
    _, _, task = await chat(agent)
    await asyncio.wait_for(task, 5)
    assert NEEDS_APPROVAL in tool_results(agent)[0]
    assert fake.paths("/v1/navigate") == [] and agent.memory._all("SELECT * FROM signin_requests") == []
    # Once that page is the one showing (opened with approval), asking needs no navigation.
    fake.url = gated
    _, _, task = await chat(agent)
    row = await waiting(agent)
    assert row["url"] == gated and fake.paths("/v1/navigate") == [] and fake.mode == "user"
    await agent.signins.cancel(row["id"])
    await asyncio.wait_for(task, 5)


async def test_sign_in_is_refused_while_roland_already_has_the_browser(tmp_path, fake):
    agent = screen_agent(tmp_path, fake, [("", [ask()]), "ok"])
    await agent.screens.start("d" * 64, "control")
    _, _, task = await chat(agent)
    await asyncio.wait_for(task, 5)
    assert "Roland is using the browser right now" in tool_results(agent)[0]
    assert agent.memory._all("SELECT * FROM signin_requests") == [] and fake.paths("/v1/navigate") == []
    # The browser going away mid-request leaves nothing half-open.
    await agent.screens.end_all("test")
    broken = screen_agent(tmp_path / "broken", fake, [("", [ask()]), "ok"])
    fake.unreachable = True
    _, _, task = await chat(broken)
    await asyncio.wait_for(task, 5)
    assert "Error:" in tool_results(broken)[0] and broken.memory.active_signin_requests() == []
    assert not broken.screens.roland_has_browser()
    # The page opened but the browser could not be handed over: the request is withdrawn at
    # once, so the agent is not left locked out of a browser Roland was never given.
    fake.unreachable = False
    fake.down_paths = {"/v1/user-mode"}
    half = screen_agent(tmp_path / "half", fake, [("", [ask()]), "ok"])
    _, events, task = await chat(half)
    await asyncio.wait_for(task, 5)
    assert "Error: the browser isn't reachable right now." in tool_results(half)[0]
    assert half.memory.active_signin_requests() == [] and not half.screens.roland_has_browser()
    row = half.memory._all("SELECT * FROM signin_requests")[0]
    assert row["status"] == "cancelled" and not any(e["type"] == "signin_required" for e in events)
    withdrawn = half.memory.audit_rows(event="signin_resolved")[0]
    assert withdrawn["actor"] == "system" and withdrawn["detail"]["reason"] == "browser_unreachable"
