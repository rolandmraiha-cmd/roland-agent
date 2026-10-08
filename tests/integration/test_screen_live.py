"""A7.3: the real screen server, x11vnc inside the browser container (marker integration).

Runs in the `tester` container of the compose test stack (tests/integration/browser.sh). The
tests talk to x11vnc directly with a small VNC client, with no noVNC in between, to check
what x11vnc itself enforces: the view-only password cannot send input, a wrong password gets
nowhere, the screen's clipboard is never sent out, and one call from core cuts every viewer.
The last test is a sign-in from start to finish, with the test playing Roland at the screen.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import socket
import time
from pathlib import Path

import httpx
import pytest
from rfb import CONTROL, DELETE, ENTER, TAB, Rfb, RfbError
from test_browser_live import (  # noqa: F401 -- the two fixtures apply to this module as well
    BROWSER_URL,
    FIXTURE_URL,
    SNAPSHOT,
    _unavailable,
    agent_for,
    browser_is_up,
    call,
    clean_start,
    open_page,
    raw,
    site_posts,
)

pytestmark = pytest.mark.integration

VNC_HOST = os.environ.get("VNC_HOST", "10.77.5.40")
VNC_PORT = 5900
SCREEN_PAGE = f"{FIXTURE_URL}/extras/screen"
# Typed "by Roland" in the sign-in test. It must reach the site and nothing else.
TYPED_SECRET = "S3cretTypedOnTheScreen"


def _password(name: str) -> str:
    path = os.environ.get(f"{name}_FILE", "")
    if path and Path(path).is_file():
        return Path(path).read_text(encoding="utf-8").strip()
    return os.environ.get(name, "")


@pytest.fixture(scope="module", autouse=True)
def screen_is_up(browser_is_up):  # noqa: F811 -- the fixture imported above
    if not _password("VNC_PASSWORD") or not _password("VNC_VIEW_PASSWORD"):
        _unavailable("the VNC password files are not available")
    health = httpx.get(f"{BROWSER_URL}/healthz", timeout=5, trust_env=False).json()
    if health.get("vnc") is not True:
        _unavailable("the screen server is not listening (is SCREEN_ENABLED=true in the browser container?)")


# --- helpers ---

def full() -> Rfb:
    """Roland in control: the password that may type and click."""
    return Rfb(VNC_HOST, VNC_PORT, _password("VNC_PASSWORD"))


def view() -> Rfb:
    """Roland watching: the view-only password."""
    return Rfb(VNC_HOST, VNC_PORT, _password("VNC_VIEW_PASSWORD"))


def screen_page() -> dict:
    """What the screen test page shows now: the field's text, the click count, the last click."""
    answer = raw("POST", "/v1/snapshot", {"max_chars": 2000}).json()
    field = next(item for item in answer["elements"] if item.get("name") == "Typed here")
    clicks = re.search(r"clicks: (\d+)", answer["text"])
    where = re.search(r"last click at (\d+),(\d+)", answer["text"])
    return {
        "typed": field["value"],
        "clicks": int(clicks.group(1)) if clicks else -1,
        "where": (int(where.group(1)), int(where.group(2))) if where else None,
    }


def wait_for(check, what: str, timeout: float = 8.0):
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.2)


def open_screen_page() -> None:
    assert raw("POST", "/v1/navigate", {"url": SCREEN_PAGE}).json()["status"] == 200


def click_into_the_page(client: Rfb) -> int:
    """Click the middle of the screen. Returns how far down the screen the page starts (the
    browser's own tabs and address bar sit above it), measured from where the click landed."""
    before = screen_page()["clicks"]
    client.click(640, 500)
    wait_for(lambda: screen_page()["clicks"] == before + 1, "the click to arrive")
    x, y = screen_page()["where"]
    assert x == 640
    return 500 - y


def distinct_colours(picture: bytes) -> int:
    return len({picture[index:index + 3] for index in range(0, len(picture), 4 * 97)})


# --- A7.3 ---

def test_vnc_view_only_password_cannot_send_input():
    """Keys and clicks sent with the view-only password never reach the page. The same keys
    sent with the full password do, so only the password made the difference."""
    open_screen_page()
    with full() as roland:
        click_into_the_page(roland)  # the keyboard is in the field now
        with view() as watcher:
            assert watcher.width == 1280 and watcher.height == 800
            assert distinct_colours(watcher.picture()) > 3  # it does see the browser
            watcher.type("no")
            watcher.key(ENTER)
            watcher.click(640, 520)
            watcher.paste("from the watcher")
            time.sleep(1.5)
            assert screen_page() == {"typed": "", "clicks": 1, "where": screen_page()["where"]}
            # Even its clipboard offer was dropped: pasting in the screen brings nothing of it.
            roland.chord(CONTROL, ord("v"))
            time.sleep(0.5)
            assert "watcher" not in screen_page()["typed"]
        roland.chord(CONTROL, ord("a"))
        roland.key(DELETE)
        roland.type("yes")
        wait_for(lambda: screen_page()["typed"] == "yes", "the full password's keys to arrive")
    assert site_posts() == []


def test_wrong_password_gets_nowhere_and_a_password_is_always_asked():
    for attempt in ("", "wrong123", _password("VNC_PASSWORD")[::-1], _password("VNC_PASSWORD")[:7]):
        with pytest.raises(RfbError, match="login failed"):
            Rfb(VNC_HOST, VNC_PORT, attempt)
    with view() as watcher:
        assert watcher.security_types == [2]  # password login, and no "none" beside it


def test_screen_clipboard_is_never_sent_to_a_viewer():
    """What is copied inside the browser stays there (it could be a password). The other
    direction works: what Roland pastes from his own device arrives."""
    open_screen_page()
    with full() as roland, view() as watcher:
        click_into_the_page(roland)
        roland.type("copyme")
        wait_for(lambda: screen_page()["typed"] == "copyme", "the typing to arrive")
        roland.chord(CONTROL, ord("a"))
        roland.chord(CONTROL, ord("c"))
        roland.key(DELETE)
        wait_for(lambda: screen_page()["typed"] == "", "the field to empty")
        roland.chord(CONTROL, ord("v"))
        wait_for(lambda: screen_page()["typed"] == "copyme", "the copy to be pasted back")  # it was copied
        roland.listen(2.5)
        watcher.listen(1.0)
        assert roland.cut_texts == [] and watcher.cut_texts == []

        roland.paste("fromphone")
        time.sleep(0.5)
        roland.chord(CONTROL, ord("a"))
        roland.chord(CONTROL, ord("v"))
        wait_for(lambda: screen_page()["typed"] == "fromphone", "Roland's own paste to arrive")
        roland.listen(1.0)
        assert roland.cut_texts == []


def test_handing_back_empties_the_screen_clipboard():
    """When Roland gives the browser back, what he copied or pasted there is gone (§6.5)."""
    open_screen_page()
    with full() as roland:
        click_into_the_page(roland)
        assert raw("POST", "/v1/user-mode", {"on": True}).json() == {"mode": "user"}
        roland.paste("left-behind")
        time.sleep(0.5)
        roland.chord(CONTROL, ord("v"))
        time.sleep(0.5)
        assert raw("POST", "/v1/snapshot", {}).status_code == 423  # the agent can't look meanwhile
        assert raw("POST", "/v1/user-mode", {"on": False}).json() == {"mode": "agent"}
        assert screen_page()["typed"] == "left-behind"  # he did paste it
        click_into_the_page(roland)
        roland.chord(CONTROL, ord("a"))
        roland.key(DELETE)
        roland.chord(CONTROL, ord("v"))
        time.sleep(0.8)
        assert screen_page()["typed"] == ""


def test_disconnect_cuts_every_viewer_and_the_screen_comes_back():
    with full() as roland, view() as watcher:
        answer = raw("POST", "/v1/vnc/disconnect")
        assert answer.status_code == 200 and answer.json() == {"ok": True, "vnc": True}
        assert not roland.alive() and not watcher.alive()
    # No waiting: the call returns once the new screen server is listening.
    with full() as again:
        assert again.width == 1280
    assert httpx.get(f"{BROWSER_URL}/healthz", timeout=5, trust_env=False).json()["vnc"] is True
    # It works while Roland has the browser too: that is when core needs it.
    assert raw("POST", "/v1/user-mode", {"on": True}).json() == {"mode": "user"}
    assert raw("POST", "/v1/vnc/disconnect").json() == {"ok": True, "vnc": True}


def test_novnc_unreachable_without_forward_auth():
    """The tester is not on the network Caddy uses to reach noVNC, and noVNC does not listen
    on the network it shares with the browser. So nothing gets to it around Caddy."""
    for host in ("10.77.2.30", "10.77.5.30"):
        with pytest.raises(OSError):
            socket.create_connection((host, 6080), timeout=3).close()


async def test_roland_signs_in_on_the_screen_and_the_agent_carries_on(agent_for):  # noqa: F811
    """A7.4 in the lab. The agent asks for a sign-in and waits. "Roland" takes control, types
    a user name and a password at the real screen and says he is done. The agent then reads
    the signed-in page. What he typed reached the site and nothing else."""
    open_screen_page()
    with full() as probe:
        page_top = click_into_the_page(probe)

    agent = agent_for(
        [
            open_page("/login"), SNAPSHOT,
            call("request_signin", url=f"{FIXTURE_URL}/login", site="the fixture site", reason="the page needs a login"),
            open_page("/whoami"), SNAPSHOT, "You are signed in as fixture-user.",
        ],
        screen_enabled=True, vnc_password=_password("VNC_PASSWORD"), vnc_view_password=_password("VNC_VIEW_PASSWORD"),
    )
    during: dict = {}

    def at_the_screen(password: str) -> Rfb:
        screen = Rfb(VNC_HOST, VNC_PORT, password)
        screen.click(40, page_top + 40)  # the page's heading: the keyboard is in the page now
        screen.key(TAB)
        screen.type("roland")
        screen.key(TAB)
        screen.type(TYPED_SECRET)
        screen.key(ENTER)
        wait_for(lambda: any(post.startswith("/login ") for post in site_posts()), "the sign-in form to be sent")
        return screen

    async def roland(signin: dict) -> None:
        try:
            during["mode"] = raw("GET", "/v1/status").json()["mode"]
            during["agent_snapshot"] = raw("POST", "/v1/snapshot", {}).status_code
            login = hashlib.sha256(b"roland's login session").hexdigest()
            during["session"] = await agent.screens.start(login, "control", signin["id"])
            screen = await asyncio.to_thread(at_the_screen, agent.screens.password_for("control"))
        except BaseException:
            await agent.signins.cancel(signin["id"])  # don't leave the agent waiting for the timeout
            raise
        try:
            await agent.signins.done(signin["id"])
            during["cut"] = not await asyncio.to_thread(screen.alive)
        finally:
            screen.close()

    chat_id = agent.memory.new_chat()
    events, tasks = [], []

    async def turn() -> None:
        async for event in agent.chat(chat_id, "who am I on the fixture site?"):
            events.append(event)
            if event["type"] == "signin_required":
                tasks.append(asyncio.create_task(roland(event["signin"])))

    await asyncio.wait_for(turn(), timeout=120)
    assert len(tasks) == 1
    await tasks[0]

    # While it waited, the agent was locked out and Roland got the control screen.
    assert during["mode"] == "user" and during["agent_snapshot"] == 423
    assert during["session"]["mode"] == "control" and during["session"]["ws_path"] == "/screen/websockify"
    assert during["cut"] is True  # "I'm done" ended his screen connection
    # His keys went to the site, through the screen.
    assert site_posts() == [f"/login username=roland&password={TYPED_SECRET}"]
    # With the screen on, asking for a sign-in is among the tools the model is offered.
    assert "request_signin" in agent.brain.tools[0]
    # The agent carried on, signed in.
    outputs = agent.brain.outputs()
    assert "Roland says he finished signing in" in outputs[2]
    assert "Signed in as fixture-user" in outputs[4]
    assert raw("GET", "/v1/status").json()["mode"] == "agent"
    # Nothing he typed is anywhere the agent keeps things: not what the model saw, not the
    # audit log, not the database, not the events sent to the chat page.
    assert all(TYPED_SECRET not in str(message) for turn_messages in agent.brain.seen for message in turn_messages)
    assert TYPED_SECRET not in str(events)
    for name in os.listdir(agent.config.data_dir):
        path = Path(agent.config.data_dir) / name
        if path.is_file():
            assert TYPED_SECRET.encode() not in path.read_bytes(), name
    happened = [row["event"] for row in agent.memory._all("SELECT event FROM audit_log ORDER BY id")]
    wanted = ["signin_requested", "screen_session_start", "signin_resolved", "screen_session_end"]
    assert [event for event in happened if event in wanted] == wanted
    assert TYPED_SECRET not in str(agent.memory._all("SELECT * FROM audit_log"))
