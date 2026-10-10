"""The screen server, x11vnc (§6.5, §6.6): its command line, its password file, and how
every screen connection is cut.

x11vnc shows the virtual screen Chromium draws on. It listens on the screen network only,
lets in the noVNC container only, and takes two passwords: one that may type and click, and
one that may only watch. x11vnc itself enforces the difference, not the page on Roland's phone.

The launcher owns the x11vnc process. To cut the connections, the service asks the launcher
(with a signal) to stop x11vnc and start a new one. A process that has ended holds no
connections, which is a firmer promise than asking a running one to drop them, and it lets
x11vnc run with its remote-control channel closed.

Nothing here logs a password, and x11vnc never logs keys (`-quiet`, never `-debug_keyboard`).
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from pathlib import Path

from .settings import VNC_PORT

PASSWD_FILE = Path("/tmp/vnc.passwd")  # noqa: S108 -- the container's private, throwaway /tmp
PID_FILE = Path("/tmp/x11vnc.pid")  # noqa: S108
VIEW_ONLY_MARK = "__BEGIN_VIEWONLY__"
# The launcher puts its process id here for the service, which signals it to restart x11vnc.
LAUNCHER_ENV = "BROWSERD_LAUNCHER_PID"
RESTART_SIGNAL = signal.SIGUSR1
RESTART_TIMEOUT_S = 6.0


def command(display: str, listen: str, peers: tuple[str, ...]) -> list[str]:
    """x11vnc's command line. Checked flag by flag against x11vnc 0.9.16 (the image's build).

    -no6 disables x11vnc's own IPv6 listener; -rfbportv6 0 also disables LibVNCServer's
    independent IPv6 listener. -allow permits only noVNC's address; -safer and -nocmds
    close remote control, reverse connections and external commands; -norc prevents a
    settings file from /tmp. Never here: -nopw, -localhost, -debug_keyboard, -passwd.
    """
    return [
        "x11vnc", "-display", display,
        "-rfbport", str(VNC_PORT), "-listen", listen, "-no6",
        "-rfbportv6", "0",
        "-allow", ",".join(peers),
        "-forever", "-shared",
        "-passwdfile", str(PASSWD_FILE),
        # The screen's clipboard is never sent out to a viewer. What a viewer pastes still arrives.
        "-noprimary", "-noclipboard",
        "-noxdamage",
        "-safer", "-nocmds", "-norc",
        "-quiet",
    ]


def environment() -> dict[str, str]:
    """What x11vnc is started with: no token, no secret file names, nothing of ours.

    X11VNC_AVOID_WINDOWS=never: x11vnc otherwise waits 45 seconds after each viewer connects
    before it will take text a viewer pastes (it guesses a login screen might still be up).
    There is no login screen here, and x11vnc is new after every screen session.
    """
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/tmp",  # noqa: S108 -- nothing is read from it (-norc)
        "X11VNC_AVOID_WINDOWS": "never",
    }


def secret_from_file(name: str) -> str:
    """The secret whose file `NAME_FILE` names, or "". Never the value or the path in an error."""
    path = os.environ.get(f"{name}_FILE", "").strip()
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""


def usable_password(value: str) -> bool:
    """One line x11vnc reads as a password: no spaces or control characters, and none of
    the markers its password file gives a meaning to (a leading "#", or "__")."""
    return (
        0 < len(value) <= 64
        and value.isascii()
        and value.isprintable()
        and " " not in value
        and not value.startswith("#")
        and "__" not in value
    )


def passwords() -> tuple[str, str] | None:
    """(full, view-only) from the mounted secret files, or None when they can't be used.
    VNC only looks at the first eight characters, so those must already differ."""
    full, view = secret_from_file("VNC_PASSWORD"), secret_from_file("VNC_VIEW_PASSWORD")
    if not usable_password(full) or not usable_password(view) or full[:8] == view[:8]:
        return None
    return full, view


def write_passwd_file(full: str, view: str, path: Path | None = None) -> None:
    """x11vnc's -passwdfile format: the full password, a marker line, the view-only password.
    Mode 0600, and never through a link someone left in /tmp."""
    path = path or PASSWD_FILE
    try:
        path.unlink()
    except OSError:
        pass
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as stream:
        stream.write(f"{full}\n{VIEW_ONLY_MARK}\n{view}\n")


def record_pid(pid: int, path: Path | None = None) -> None:
    """Which x11vnc is the current one. Replaced in one step, so a reader never sees half."""
    path = path or PID_FILE
    temporary = path.with_name(path.name + ".new")
    temporary.write_text(f"{pid}\n", encoding="ascii")
    os.replace(temporary, path)


def current_pid(path: Path | None = None, proc: Path = Path("/proc")) -> int | None:
    """The process id of the x11vnc the launcher last started, while that process is alive."""
    path = path or PID_FILE
    try:
        pid = int(path.read_text(encoding="ascii").strip())
        name = (proc / str(pid) / "comm").read_text(encoding="ascii").strip()
    except (OSError, ValueError):
        return None
    return pid if pid > 1 and name == "x11vnc" else None


def listening(host: str, port: int = VNC_PORT, table: Path = Path("/proc/net/tcp")) -> bool:
    """True when something in this container listens on host:port. Read from the kernel's
    table, so checking never opens a connection to the screen server."""
    try:
        packed = bytes(int(part) for part in host.split("."))
        if len(packed) != 4:
            return False
        # The table prints the address as one number in the machine's own byte order.
        shown = packed[::-1] if sys.byteorder == "little" else packed
        want = f"{shown.hex().upper()}:{port:04X}"
        lines = table.read_text(encoding="ascii").splitlines()[1:]
    except (OSError, ValueError):
        return False
    for line in lines:
        fields = line.split()
        if len(fields) > 3 and fields[1].upper() == want and fields[3].upper() == "0A":
            return True
    return False


def launcher_pid() -> int | None:
    """The launcher to signal, or None when this service has no launcher running x11vnc
    (the screen is off, its passwords are unusable, or this is a test run)."""
    raw = os.environ.get(LAUNCHER_ENV, "").strip()
    if not raw.isdigit() or int(raw) <= 1:
        return None
    # Only ever our own parent: SIGUSR1 would end any other process it reached.
    return int(raw) if int(raw) == os.getppid() else None


async def cut_connections(listen: str, *, timeout: float = RESTART_TIMEOUT_S) -> str:
    """End every screen connection by having the launcher replace x11vnc.

    "none": no screen server is running, so nothing was connected (one is asked for anyway).
    "cut": the old process is gone and a new one is listening.
    "failed": that didn't happen in time; core tries again.
    """
    parent = launcher_pid()
    if parent is None:
        return "none"
    old = current_pid()
    try:
        os.kill(parent, RESTART_SIGNAL)
    except OSError:
        return "failed"
    if old is None and not listening(listen):
        return "none"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        now = current_pid()
        if now is not None and now != old and listening(listen):
            return "cut"
    return "failed"
