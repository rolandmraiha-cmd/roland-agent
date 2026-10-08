"""The screen server in the browser container (M7.1, §6.5): x11vnc's command line and password
file, how every screen connection is cut, and how the launcher looks after it.

No x11vnc runs here. The real one, with a real viewer, is covered by
tests/integration/test_screen_live.py; the DES the viewer needs is checked at the bottom.
"""

from __future__ import annotations

import os
import signal
import stat
import sys
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport

from browserd import launcher, settings, vnc
from browserd.server import OPEN_IN_USER_MODE, create_app, healthcheck_cli

sys.path.insert(0, str(Path(__file__).parent / "integration"))
import rfb  # noqa: E402 -- the tests' own small VNC client

TOKEN = "unit-test-token-0123456789"
FULL, VIEW = "Fu11pw9Z", "V1ewpw7Q"


# --- the command line ---

def test_x11vnc_listens_on_the_screen_network_only_and_never_without_a_password():
    argv = vnc.command(":99", "10.77.5.40", ("10.77.5.30",))
    assert argv[0] == "x11vnc"

    def value(flag):
        return argv[argv.index(flag) + 1]

    assert value("-display") == ":99" and value("-rfbport") == "5900"
    assert value("-listen") == "10.77.5.40" and "-no6" in argv  # one IPv4 address, nothing on IPv6
    assert value("-allow") == "10.77.5.30"                      # only noVNC may connect
    assert value("-passwdfile") == "/tmp/vnc.passwd"
    # The spec's flags (§6.5), each present.
    for flag in ("-forever", "-shared", "-noprimary", "-noclipboard", "-noxdamage", "-quiet"):
        assert flag in argv, flag
    # No remote control, no reverse connections, no external commands, no settings file.
    for flag in ("-safer", "-nocmds", "-norc"):
        assert flag in argv, flag
    # Never: no password, a password on the command line, key logging, a web server of its
    # own, file transfer, or the loopback-only switch that would hide it from noVNC.
    for banned in ("-nopw", "-passwd", "-viewpasswd", "-rfbauth", "-localhost", "-debug_keyboard", "-dk", "-debug_pointer",
                   "-http", "-httpdir", "-httpport", "-ssl", "-unixpw", "-bg", "-connect", "-reflect", "-gui",
                   "-tightfilexfer", "-ultrafilexfer", "-permitfiletransfer", "-yesremote", "-unsafe", "-o", "-logfile"):
        assert banned not in argv, banned
    assert not any(FULL in part or VIEW in part for part in argv)
    # Several peers are joined the way x11vnc reads them (the test stack adds the tester).
    argv = vnc.command(":7", "10.0.0.1", ("10.77.5.30", "10.77.5.31"))
    assert value("-allow") == "10.77.5.30,10.77.5.31" and value("-display") == ":7" and value("-listen") == "10.0.0.1"


def test_x11vnc_is_started_with_nothing_of_ours_in_its_environment(monkeypatch):
    monkeypatch.setenv("BROWSER_API_TOKEN_FILE", "/run/secrets/browser_api_token")
    monkeypatch.setenv("VNC_PASSWORD_FILE", "/run/secrets/vnc_password")
    env = vnc.environment()
    assert set(env) == {"PATH", "HOME", "X11VNC_AVOID_WINDOWS"}
    assert env["X11VNC_AVOID_WINDOWS"] == "never"  # pasting works at once, not after 45 seconds
    assert "secret" not in str(env).lower() and "TOKEN" not in str(env)


# --- the passwords ---

@pytest.mark.parametrize(
    ("value", "usable"),
    [
        (FULL, True), ("control-test", True), ("a", True), ("p@ss:w0rd!", True), ("x" * 64, True),
        ("", False), ("x" * 65, False), ("two words", False), ("tab\there", False), ("line\nbreak", False),
        ("#comment", False), ("a__SKIP__b", False), ("__BEGIN_VIEWONLY__", False), ("__EMPTY__", False),
        ("pässword", False), ("pw\x00", False), ("pw\x7f", False),
    ],
)
def test_only_passwords_x11vnc_reads_as_one_plain_line_are_used(value, usable):
    assert vnc.usable_password(value) is usable


def write_secrets(tmp_path, monkeypatch, full=FULL, view=VIEW):
    for name, value in (("VNC_PASSWORD", full), ("VNC_VIEW_PASSWORD", view)):
        path = tmp_path / name.lower()
        if value is None:
            monkeypatch.setenv(f"{name}_FILE", str(tmp_path / "missing"))
            continue
        path.write_text(value)
        monkeypatch.setenv(f"{name}_FILE", str(path))


def test_passwords_come_from_the_mounted_files_and_must_differ_where_vnc_looks(tmp_path, monkeypatch):
    write_secrets(tmp_path, monkeypatch, FULL + "\n", VIEW + "\n")  # a trailing newline is not part of it
    assert vnc.passwords() == (FULL, VIEW)
    # VNC compares eight characters: alike there, the view-only password would be the full one.
    write_secrets(tmp_path, monkeypatch, "Abcdefgh-control", "Abcdefgh-view")
    assert vnc.passwords() is None
    write_secrets(tmp_path, monkeypatch, FULL, FULL)
    assert vnc.passwords() is None
    for full, view in ((None, VIEW), (FULL, None), ("", VIEW), (FULL, "two words"), ("#x", VIEW)):
        write_secrets(tmp_path, monkeypatch, full, view)
        assert vnc.passwords() is None, (full, view)
    # Only files: a value in the environment is not picked up.
    monkeypatch.delenv("VNC_PASSWORD_FILE")
    monkeypatch.setenv("VNC_PASSWORD", FULL)
    assert vnc.secret_from_file("VNC_PASSWORD") == ""


def test_the_password_file_is_private_and_in_x11vncs_format(tmp_path):
    path = tmp_path / "vnc.passwd"
    vnc.write_passwd_file(FULL, VIEW, path)
    assert path.read_text() == f"{FULL}\n__BEGIN_VIEWONLY__\n{VIEW}\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # Written again on every start, with whatever the secret files hold then.
    vnc.write_passwd_file("N3wpw123", VIEW, path)
    assert path.read_text().splitlines()[0] == "N3wpw123"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # A link left at that name is replaced, not followed.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text("untouched")
    path.unlink()
    path.symlink_to(elsewhere)
    vnc.write_passwd_file(FULL, VIEW, path)
    assert elsewhere.read_text() == "untouched" and not path.is_symlink()
    assert path.read_text().startswith(FULL)


# --- which x11vnc is running, and is it listening ---

def test_the_current_x11vnc_is_known_by_its_recorded_pid(tmp_path):
    pid_file, proc = tmp_path / "x11vnc.pid", tmp_path / "proc"
    assert vnc.current_pid(pid_file, proc) is None  # nothing recorded
    vnc.record_pid(4321, pid_file)
    assert pid_file.read_text() == "4321\n" and not list(tmp_path.glob("*.new"))
    assert vnc.current_pid(pid_file, proc) is None  # recorded, but no such process
    (proc / "4321").mkdir(parents=True)
    (proc / "4321" / "comm").write_text("python3\n")
    assert vnc.current_pid(pid_file, proc) is None  # that number belongs to something else now
    (proc / "4321" / "comm").write_text("x11vnc\n")
    assert vnc.current_pid(pid_file, proc) == 4321
    for junk in ("", "abc", "-5", "1", "0"):
        pid_file.write_text(junk)
        assert vnc.current_pid(pid_file, proc) is None, junk


def test_listening_is_read_from_the_kernels_table_without_connecting(tmp_path):
    table = tmp_path / "tcp"
    # As seen in the real container: Docker's resolver, browserd, x11vnc, and connections.
    table.write_text(
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        "   0: 0B00007F:8E31 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 45238 2\n"
        "   1: 28044D0A:1BBC 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 45619 1\n"
        "   2: 28054D0A:170C 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 45336 1\n"
        "   3: 28054D0A:170D 1E054D0A:C770 01 00000000:00000000 03:0000176F 00000000  1000        0 0 3\n"
    )
    if sys.byteorder == "little":
        assert vnc.listening("10.77.5.40", 5900, table) is True
        assert vnc.listening("10.77.4.40", 7100, table) is True
        assert vnc.listening("10.77.5.40", 5901, table) is False  # a connection there, nothing listening
    assert vnc.listening("10.77.5.41", 5900, table) is False
    assert vnc.listening("10.77.5.40", 6080, table) is False
    assert vnc.listening("0.0.0.0", 5900, table) is False  # noqa: S104 -- asserting it is NOT bound to every address
    for bad in ("", "10.77.5", "host", "10.77.5.400"):
        assert vnc.listening(bad, 5900, table) is False
    assert vnc.listening("10.77.5.40", 5900, tmp_path / "missing") is False


# --- cutting every screen connection ---

def test_the_service_only_ever_signals_its_own_parent(monkeypatch):
    monkeypatch.delenv(vnc.LAUNCHER_ENV, raising=False)
    assert vnc.launcher_pid() is None
    monkeypatch.setattr(os, "getppid", lambda: 77)
    for raw in ("", "abc", "0", "1", "76", "-77", "77 "):
        monkeypatch.setenv(vnc.LAUNCHER_ENV, raw)
        assert vnc.launcher_pid() == (77 if raw.strip() == "77" else None), raw
    monkeypatch.setenv(vnc.LAUNCHER_ENV, "77")
    assert vnc.launcher_pid() == 77
    assert vnc.RESTART_SIGNAL == signal.SIGUSR1


class Stage:
    """Stands in for the launcher and its x11vnc: what a signal to the launcher leads to."""

    def __init__(self, monkeypatch, *, pid=100, listening=True, comes_back=True, kill_fails=False):
        self.pid, self.is_listening, self.comes_back, self.kill_fails = pid, listening, comes_back, kill_fails
        self.signals: list[tuple[int, int]] = []
        monkeypatch.setattr(vnc, "launcher_pid", lambda: 77)
        monkeypatch.setattr(vnc, "current_pid", lambda *args: self.pid)
        monkeypatch.setattr(vnc, "listening", lambda *args: self.is_listening)
        monkeypatch.setattr(os, "kill", self.kill)

    def kill(self, pid, sig):
        self.signals.append((pid, sig))
        if self.kill_fails:
            raise ProcessLookupError
        if self.pid is not None and self.comes_back:
            self.pid += 1  # the launcher ended x11vnc and started a new one
        elif self.pid is not None:
            self.pid, self.is_listening = None, False


async def test_cutting_connections_replaces_x11vnc_and_waits_for_the_new_one(monkeypatch):
    stage = Stage(monkeypatch)
    assert await vnc.cut_connections("10.77.5.40", timeout=1) == "cut"
    assert stage.signals == [(77, signal.SIGUSR1)] and stage.pid == 101


async def test_cutting_connections_reports_failure_when_x11vnc_does_not_come_back(monkeypatch):
    stage = Stage(monkeypatch, comes_back=False)
    assert await vnc.cut_connections("10.77.5.40", timeout=0.3) == "failed"
    assert stage.signals == [(77, signal.SIGUSR1)]
    # The same when the old one never goes away ...
    stuck = Stage(monkeypatch)
    stuck.kill = lambda pid, sig: stuck.signals.append((pid, sig))
    monkeypatch.setattr(os, "kill", stuck.kill)
    assert await vnc.cut_connections("10.77.5.40", timeout=0.3) == "failed"
    # ... or a new one is there but not listening yet.
    deaf = Stage(monkeypatch)
    original = deaf.kill

    def kill(pid, sig):
        original(pid, sig)
        deaf.is_listening = False

    monkeypatch.setattr(os, "kill", kill)
    assert await vnc.cut_connections("10.77.5.40", timeout=0.3) == "failed"
    # The launcher is gone.
    gone = Stage(monkeypatch, kill_fails=True)
    assert await vnc.cut_connections("10.77.5.40", timeout=0.3) == "failed" and len(gone.signals) == 1


async def test_with_no_screen_server_running_there_is_nothing_to_cut(monkeypatch):
    # x11vnc is down (its passwords are unusable, or it is waiting to be started again):
    # no process, no connections. One is asked for all the same, and the answer is at once.
    stage = Stage(monkeypatch, pid=None, listening=False)
    assert await vnc.cut_connections("10.77.5.40", timeout=5) == "none"
    assert stage.signals == [(77, signal.SIGUSR1)]
    # No launcher at all (the screen is off, or a test run): nothing is signalled.
    monkeypatch.setattr(vnc, "launcher_pid", lambda: None)
    assert await vnc.cut_connections("10.77.5.40", timeout=5) == "none"
    assert stage.signals == [(77, signal.SIGUSR1)]


# --- the route core calls ---

class Session:
    mode = "agent"

    def health(self):
        return {"ok": True, "xvfb": True, "vnc": False, "browser": True}


def app_with(outcomes: list[str], **changes):
    asked: list[str] = []

    async def cut(listen: str) -> str:
        asked.append(listen)
        return outcomes.pop(0)

    config = settings.Settings(token=TOKEN, **changes)
    return create_app(config, Session(), cut_screen=cut), asked


def client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=ASGITransport(app=app, client=("10.77.4.10", 40000)), base_url="http://browser.test",
        headers={"Authorization": f"Bearer {TOKEN}"}, trust_env=False,
    )


async def test_disconnect_route_cuts_the_screen_and_says_what_happened():
    assert "/v1/vnc/disconnect" in OPEN_IN_USER_MODE  # core needs it exactly while Roland has the browser
    app, asked = app_with(["cut", "none", "failed"], screen_enabled=True, vnc_listen="10.77.5.40")
    async with client(app) as http:
        assert (await http.post("/v1/vnc/disconnect")).json() == {"ok": True, "vnc": True}
        assert (await http.post("/v1/vnc/disconnect")).json() == {"ok": True, "vnc": False}
        failed = await http.post("/v1/vnc/disconnect")
        # Not "ok": core must keep trying, or a page left open could go on watching.
        assert failed.status_code == 503 and failed.json() == {"error": "unavailable"}
    assert asked == ["10.77.5.40"] * 3
    # With the screen switched off nothing is asked of the launcher at all.
    app, asked = app_with([], screen_enabled=False)
    async with client(app) as http:
        assert (await http.post("/v1/vnc/disconnect")).json() == {"ok": True, "vnc": False}
    assert asked == []
    # Still for core only.
    app, asked = app_with(["cut"], screen_enabled=True)
    async with httpx.AsyncClient(transport=ASGITransport(app=app, client=("10.77.5.30", 1)), base_url="http://b") as http:
        assert (await http.post("/v1/vnc/disconnect", headers={"Authorization": f"Bearer {TOKEN}"})).status_code == 403
    async with httpx.AsyncClient(transport=ASGITransport(app=app, client=("10.77.4.10", 1)), base_url="http://b") as http:
        assert (await http.post("/v1/vnc/disconnect")).status_code == 401
    assert asked == []


def test_health_says_whether_the_screen_server_listens_without_deciding_ok(monkeypatch):
    from browserd.session import Session as RealSession

    seen = []
    monkeypatch.setattr(vnc, "listening", lambda host, *rest: seen.append(host) or True)
    on = RealSession(settings.Settings(token=TOKEN, headless=True, screen_enabled=True, vnc_listen="10.77.5.40"))
    assert on.health()["vnc"] is True and seen == ["10.77.5.40"]
    monkeypatch.setattr(vnc, "listening", lambda *args: False)
    health = on.health()
    assert health["vnc"] is False and health["ok"] is health["browser"]  # the browser's state alone decides "ok"
    off = RealSession(settings.Settings(token=TOKEN, headless=True))
    monkeypatch.setattr(vnc, "listening", lambda *args: pytest.fail("not asked while the screen is off"))
    assert off.health()["vnc"] is False


class FrontPage:
    """A tab that only knows how to be put in front."""

    url = "https://shop.example/login"

    def __init__(self, broken=False):
        self.fronted = 0
        self.broken = broken
        self.frames = []
        self.main_frame = self

    async def bring_to_front(self):
        if self.broken:
            raise RuntimeError("Target page, context or browser has been closed")
        self.fronted += 1

    async def title(self):
        return "Sign in"


async def test_the_agents_tab_is_put_in_front_when_the_screen_changes_hands():
    """What Roland sees on the screen is the browser window. A tab the agent can't see may
    have opened in front of the one it is on (found with the real browser: three blank tabs
    covered the sign-in page), so the agent's tab is fronted whenever core says who has the
    browser, and after every action."""
    from browserd.session import Session as RealSession
    from browserd.session import Tab, _Action

    browser = RealSession(settings.Settings(token=TOKEN))  # headed, as in production
    behind, current = FrontPage(), FrontPage()
    browser._tabs = {"t1": Tab("t1", behind, number=1), "t2": Tab("t2", current, number=2)}
    browser._active = "t2"

    async def nothing():
        return None

    browser._hand_back = nothing
    assert await browser.set_user_mode(True) == {"mode": "user"}   # Roland takes control, or a sign-in starts
    assert (current.fronted, behind.fronted) == (1, 0)
    assert await browser.set_user_mode(False) == {"mode": "agent"}  # handed back, or a watch session starts
    assert (current.fronted, behind.fronted) == (2, 0)
    # After an action, too: the answer describes the tab that is now in front.
    act = _Action(browser._tabs["t2"], current.url, 0, approved=False, tabs_before=2)
    answer = await browser._answer(act)
    assert answer["url"] == current.url and current.fronted == 3
    await browser._answer()  # a plain status question moves nothing
    assert current.fronted == 3
    # A tab that is closing doesn't break the hand-over.
    current.broken = True
    assert await browser.set_user_mode(True) == {"mode": "user"}
    # Without a window (tests, development) there is nothing to put in front.
    headless = RealSession(settings.Settings(token=TOKEN, headless=True))
    page = FrontPage()
    headless._tabs = {"t1": Tab("t1", page, number=1)}
    headless._active = "t1"
    headless._hand_back = nothing
    await headless.set_user_mode(True)
    assert page.fronted == 0


@pytest.mark.parametrize(
    ("payload", "plain", "with_screen"),
    [
        ({"ok": True, "browser": True, "vnc": True}, True, True),
        ({"ok": True, "browser": True, "vnc": False}, True, False),
        ({"ok": True, "browser": True}, True, False),
        ({"ok": False, "browser": False, "vnc": True}, False, False),
    ],
)
def test_the_health_check_can_ask_for_the_screen_server_too(monkeypatch, payload, plain, with_screen):
    import io
    import json
    import urllib.request

    class Opener:
        def open(self, url, timeout):
            assert url == "http://10.77.4.40:7100/healthz"
            return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: Opener())
    monkeypatch.delenv("BROWSERD_HOST", raising=False)
    monkeypatch.delenv("BROWSERD_PORT", raising=False)
    assert healthcheck_cli() is plain
    assert healthcheck_cli(screen=True) is with_screen


# --- settings ---

def env_for(monkeypatch, **values):
    for name in ("SCREEN_ENABLED", "VNC_LISTEN", "VNC_ALLOWED_PEERS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BROWSER_API_TOKEN", TOKEN)
    monkeypatch.delenv("BROWSER_API_TOKEN_FILE", raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_the_screen_is_off_by_default_and_listens_where_compose_puts_it(monkeypatch):
    env_for(monkeypatch)
    config = settings.Settings.from_env()
    assert config.screen_enabled is False
    assert config.vnc_listen == "10.77.5.40" and config.vnc_peers == ("10.77.5.30",)
    env_for(monkeypatch, SCREEN_ENABLED="true", VNC_LISTEN="10.9.9.9", VNC_ALLOWED_PEERS="10.77.5.30, 10.77.5.31")
    config = settings.Settings.from_env()
    assert config.screen_enabled is True
    assert config.vnc_listen == "10.9.9.9" and config.vnc_peers == ("10.77.5.30", "10.77.5.31")
    assert settings.VNC_PORT == 5900


@pytest.mark.parametrize("listen", ["0.0.0.0", "127.0.0.1", "localhost", "::", "10.77.5", "10.77.5.40:5900", "browser"])  # noqa: S104
def test_the_screen_server_never_listens_everywhere_or_on_loopback(monkeypatch, listen):
    env_for(monkeypatch, VNC_LISTEN=listen)
    with pytest.raises(SystemExit, match="VNC_LISTEN"):
        settings.Settings.from_env()


@pytest.mark.parametrize("peers", ["10.77.5.", "/etc/hosts", "novnc", "10.77.5.30,*", "10.77.5.30/24", ",", "::1"])
def test_only_whole_addresses_may_be_let_in(monkeypatch, peers):
    """x11vnc's allow list also takes address prefixes and file names; neither gets that far."""
    env_for(monkeypatch, VNC_ALLOWED_PEERS=peers)
    with pytest.raises(SystemExit, match="VNC_ALLOWED_PEERS"):
        settings.Settings.from_env()


# --- the launcher ---

def test_the_launcher_adds_x11vnc_only_when_the_screen_is_on(monkeypatch):
    for name in ("SCREEN_ENABLED", "VNC_LISTEN", "VNC_ALLOWED_PEERS"):
        monkeypatch.delenv(name, raising=False)
    assert launcher.extra_children(":99") == []
    monkeypatch.setenv("SCREEN_ENABLED", "false")
    assert launcher.extra_children(":99") == []
    monkeypatch.setenv("SCREEN_ENABLED", "true")
    (child,) = launcher.extra_children(":99")
    assert child.name == "x11vnc" and child.needs_screen is True  # started once the virtual screen is up
    assert child.argv == vnc.command(":99", "10.77.5.40", ("10.77.5.30",))
    monkeypatch.setenv("VNC_LISTEN", "0.0.0.0")  # noqa: S104 -- must be refused
    with pytest.raises(SystemExit, match="VNC_LISTEN"):
        launcher.extra_children(":99")


class FakeProcess:
    """A child process the launcher thinks it started."""

    made: list[FakeProcess] = []
    next_pid = 500

    def __init__(self, argv, env=None):
        FakeProcess.next_pid += 1
        self.argv, self.env, self.pid = argv, env, FakeProcess.next_pid
        self.returncode = None
        self.signals: list[str] = []
        FakeProcess.made.append(self)

    def poll(self):
        return self.returncode

    def terminate(self):
        self.signals.append("term")
        if not getattr(self, "ignores_term", False):
            self.returncode = 2  # x11vnc leaves with 2 on SIGTERM

    def kill(self):
        self.signals.append("kill")
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


DISPLAY = ":7391"  # no real screen has this number, so nothing real is ever touched


def run_launcher(monkeypatch, tmp_path, script, *, secrets=True, screen=True):
    """Run the launcher's loop against fake processes. `script(clock, handlers, made)` is called
    once per loop round and returns True to stop the launcher."""
    FakeProcess.made = []
    handlers: dict = {}
    clock = {"now": 1000.0, "tick": 0}
    monkeypatch.setenv("DISPLAY", DISPLAY)
    monkeypatch.setenv("SCREEN_ENABLED", "true" if screen else "false")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("BROWSERD_HEADLESS", raising=False)
    monkeypatch.delenv("VNC_LISTEN", raising=False)
    monkeypatch.delenv("VNC_ALLOWED_PEERS", raising=False)
    if secrets:
        write_secrets(tmp_path, monkeypatch)
    else:
        write_secrets(tmp_path, monkeypatch, None, None)
    monkeypatch.setattr(vnc, "PASSWD_FILE", tmp_path / "vnc.passwd")
    monkeypatch.setattr(vnc, "PID_FILE", tmp_path / "x11vnc.pid")
    monkeypatch.setattr(launcher.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(launcher.signal, "signal", lambda number, handler: handlers.__setitem__(number, handler))
    monkeypatch.setattr(launcher, "screen_socket", lambda display: tmp_path)  # "exists": the screen is up
    monkeypatch.setattr(launcher.time, "monotonic", lambda: clock["now"])

    def sleep(seconds):
        clock["now"] += seconds
        clock["tick"] += 1
        if clock["tick"] > 2000:
            raise AssertionError("the launcher loop did not finish")
        if script(clock, handlers, FakeProcess.made):
            handlers[signal.SIGTERM](signal.SIGTERM, None)

    monkeypatch.setattr(launcher.time, "sleep", sleep)
    assert launcher.run() == 0
    return FakeProcess.made, handlers


def named(made, name):
    return [process for process in made if process.argv[0] == name or (name == "browserd" and "browserd" in process.argv)]


def test_the_launcher_starts_x11vnc_with_its_password_file_and_tells_the_service(monkeypatch, tmp_path):
    made, handlers = run_launcher(monkeypatch, tmp_path, lambda clock, handlers, made: clock["tick"] >= 3)
    assert [process.argv[0] for process in made[:2]] == ["Xvfb", "x11vnc"]
    (screen,) = named(made, "x11vnc")
    assert screen.argv == vnc.command(DISPLAY, "10.77.5.40", ("10.77.5.30",))
    assert screen.env == vnc.environment()  # none of the launcher's own settings
    assert (tmp_path / "vnc.passwd").read_text() == f"{FULL}\n__BEGIN_VIEWONLY__\n{VIEW}\n"
    assert (tmp_path / "x11vnc.pid").read_text() == f"{screen.pid}\n"
    (service,) = named(made, "browserd")
    assert service.env[vnc.LAUNCHER_ENV] == str(os.getpid())  # whom to ask for a restart
    assert vnc.RESTART_SIGNAL in handlers and signal.SIGTERM in handlers
    # Stopping ends the service first and everything else after it.
    assert all(process.signals == ["term"] for process in made)


def test_a_restart_request_replaces_x11vnc_at_once_and_only_x11vnc(monkeypatch, tmp_path):
    def script(clock, handlers, made):
        if clock["tick"] in {5, 9, 13}:  # three times in a row, as when Roland switches modes
            handlers[vnc.RESTART_SIGNAL](vnc.RESTART_SIGNAL, None)
        return clock["tick"] >= 20

    made, _ = run_launcher(monkeypatch, tmp_path, script)
    screens = named(made, "x11vnc")
    assert len(screens) == 4  # the first and one per request: no waiting in between
    assert [screen.signals for screen in screens[:3]] == [["term"]] * 3
    assert len(named(made, "Xvfb")) == 1 and len(named(made, "browserd")) == 1
    assert (tmp_path / "x11vnc.pid").read_text() == f"{screens[-1].pid}\n"
    # The password file is written anew for each one.
    assert (tmp_path / "vnc.passwd").read_text().startswith(FULL)


def test_an_x11vnc_that_ignores_the_request_is_killed(monkeypatch, tmp_path):
    def script(clock, handlers, made):
        screens = named(made, "x11vnc")
        if clock["tick"] == 3:
            screens[0].ignores_term = True
            handlers[vnc.RESTART_SIGNAL](vnc.RESTART_SIGNAL, None)
        return clock["tick"] >= 60

    made, _ = run_launcher(monkeypatch, tmp_path, script)
    screens = named(made, "x11vnc")
    assert screens[0].signals == ["term", "kill"] and len(screens) == 2


def test_an_x11vnc_that_dies_by_itself_is_started_again_more_slowly(monkeypatch, tmp_path):
    def script(clock, handlers, made):
        for screen in named(made, "x11vnc"):
            if screen.returncode is None and clock["tick"] > 2:
                screen.returncode = 1  # it keeps crashing
        return clock["now"] >= 1000 + 20

    made, _ = run_launcher(monkeypatch, tmp_path, script)
    # In 20 seconds: at once, then after 2, 4 and 8 seconds. Not one per loop round.
    assert 3 <= len(named(made, "x11vnc")) <= 5
    assert len(named(made, "browserd")) == 1  # the agent's browser is not disturbed


def test_a_restart_request_does_not_wait_out_an_earlier_crash(monkeypatch, tmp_path):
    """x11vnc crashed and is waiting to be started again more slowly. Core then asks for the
    connections to be cut: the new one starts on the next round, not after the wait."""
    seen = {}

    def script(clock, handlers, made):
        screens = named(made, "x11vnc")
        if clock["tick"] in {3, 30}:  # crashes at once, twice: the next start is 4 seconds off
            screens[-1].returncode = 1
        if clock["tick"] == 33:
            seen["before"] = len(screens)
            handlers[vnc.RESTART_SIGNAL](vnc.RESTART_SIGNAL, None)
        if clock["tick"] == 35:
            seen["after"] = len(screens)
        return clock["tick"] >= 36

    run_launcher(monkeypatch, tmp_path, script)
    assert seen == {"before": 2, "after": 3}


def test_without_usable_passwords_the_screen_stays_off_and_the_browser_still_runs(monkeypatch, tmp_path, caplog):
    def script(clock, handlers, made):
        if clock["tick"] == 5:
            handlers[vnc.RESTART_SIGNAL](vnc.RESTART_SIGNAL, None)  # asked for: tried again, still nothing
        return clock["tick"] >= 40

    with caplog.at_level("ERROR"):
        made, _ = run_launcher(monkeypatch, tmp_path, script, secrets=False)
    assert named(made, "x11vnc") == [] and not (tmp_path / "vnc.passwd").exists()
    assert len(named(made, "Xvfb")) == 1 and len(named(made, "browserd")) == 1
    said = [record.message for record in caplog.records if "screen stays off" in record.message]
    assert 1 <= len(said) <= 2 and all("make secrets" in line for line in said)
    assert FULL not in caplog.text and VIEW not in caplog.text


def test_with_the_screen_off_the_service_gets_no_launcher_to_signal(monkeypatch, tmp_path):
    monkeypatch.setenv(vnc.LAUNCHER_ENV, "12345")  # a stale value from outside must not get through

    def script(clock, handlers, made):
        if clock["tick"] == 2:
            # Still handled, so that the signal could never end the launcher. It does nothing.
            handlers[vnc.RESTART_SIGNAL](vnc.RESTART_SIGNAL, None)
        return clock["tick"] >= 5

    made, handlers = run_launcher(monkeypatch, tmp_path, script, screen=False)
    assert named(made, "x11vnc") == [] and not (tmp_path / "vnc.passwd").exists()
    assert len(named(made, "Xvfb")) == 1
    (service,) = named(made, "browserd")
    assert vnc.LAUNCHER_ENV not in service.env
    assert vnc.RESTART_SIGNAL in handlers


# --- the tests' own VNC client ---

def test_the_test_clients_des_matches_the_published_vectors():
    # FIPS 46 / NBS SP 500-20 known answers.
    assert rfb.des_encrypt_block(bytes.fromhex("133457799BBCDFF1"), bytes.fromhex("0123456789ABCDEF")).hex() == "85e813540f0ab405"
    assert rfb.des_encrypt_block(bytes.fromhex("0101010101010101"), bytes.fromhex("8000000000000000")).hex() == "95f8a5e5dd31d900"
    assert rfb.des_encrypt_block(bytes.fromhex("0101010101010101"), bytes.fromhex("0000000000000001")).hex() == "166b40b44aba4bd6"
    # A VNC login answer, checked against OpenSSL's DES when this client was written.
    assert rfb.vnc_response(FULL, bytes(range(16))).hex() == "eee01d60e2c65afed3a5883ed0a9d4ad"
    # VNC uses eight characters and no more.
    assert rfb.vnc_response(FULL + "ignored", bytes(range(16))) == rfb.vnc_response(FULL, bytes(range(16)))
    assert rfb.vnc_response(FULL[:7], bytes(range(16))) != rfb.vnc_response(FULL, bytes(range(16)))
    with pytest.raises(ValueError):
        rfb.vnc_response(FULL, b"short")
