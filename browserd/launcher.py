"""Starts and looks after browserd's processes (§6.5): the virtual screen, the screen server
(x11vnc, only while SCREEN_ENABLED is true) and the service.

A process that exits is started again, waiting longer each time up to 30 seconds. The one
exception is x11vnc when the service asked for it to be replaced (`vnc.RESTART_SIGNAL`): that
is how every screen connection is cut, so the new one starts at once.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import vnc
from .settings import _address, _addresses, _bool, _viewport

log = logging.getLogger("browserd.launcher")
MAX_BACKOFF_S = 30
STEADY_S = 60  # a process that ran this long starts again without delay
STOP_GRACE_S = 2  # x11vnc leaves within milliseconds; after this long it is killed


@dataclass
class Child:
    name: str
    argv: list[str]
    needs_screen: bool = False
    process: subprocess.Popen | None = None
    failures: int = 0
    not_before: float = 0.0
    started: float = 0.0
    restart: bool = False     # asked to stop and start again at once (x11vnc only)
    stop_sent: float = 0.0

    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None


def screen_socket(display: str) -> Path:
    return Path(f"/tmp/.X11-unix/X{display.lstrip(':')}")  # noqa: S108 -- where X servers listen


def xvfb_child(display: str, width: int, height: int) -> Child:
    return Child("xvfb", [
        "Xvfb", display, "-screen", "0", f"{width}x{height}x24", "-nolisten", "tcp", "-noreset",
    ])


def extra_children(display: str) -> list[Child]:
    """The screen server (M7), when the screen is switched on. Its passwords are read each
    time it starts, in `run`, so they are never kept here or put on a command line."""
    if not _bool("SCREEN_ENABLED"):
        return []
    listen = _address("VNC_LISTEN", "10.77.5.40")
    peers = _addresses("VNC_ALLOWED_PEERS", "10.77.5.30")
    return [Child("x11vnc", vnc.command(display, listen, peers), needs_screen=True)]


def backoff(failures: int) -> float:
    return float(min(MAX_BACKOFF_S, 2 ** min(failures, 6))) if failures else 0.0


def run() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    display = os.environ.get("DISPLAY", "").strip() or ":99"
    headless = _bool("BROWSERD_HEADLESS")
    width, height = _viewport()
    service = Child("browserd", [sys.executable, "-m", "browserd", "serve"], needs_screen=not headless)
    children = ([] if headless else [xvfb_child(display, width, height), *extra_children(display)]) + [service]
    screen = next((child for child in children if child.name == "x11vnc"), None)
    stopping = []

    def stop(_signum, _frame) -> None:
        stopping.append(True)

    def replace_screen(_signum, _frame) -> None:
        """The service wants every screen connection cut: end x11vnc; the loop starts a new one."""
        if screen is None:
            return
        screen.restart = True
        screen.stop_sent = time.monotonic()
        if screen.running():
            screen.process.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    # Always handled, so that the signal can never end the launcher itself.
    signal.signal(vnc.RESTART_SIGNAL, replace_screen)
    # The service may only signal a launcher that really runs x11vnc.
    service_env = dict(os.environ)
    service_env.pop(vnc.LAUNCHER_ENV, None)
    if screen is not None:
        service_env[vnc.LAUNCHER_ENV] = str(os.getpid())
    # HOME is on the container's throwaway /tmp, which starts empty.
    Path(os.environ.get("HOME") or "/tmp/home").mkdir(parents=True, exist_ok=True)  # noqa: S108

    while not stopping:
        now = time.monotonic()
        screen_up = headless or (children[0].running() and screen_socket(display).exists())
        for child in children:
            if child.running():
                if child.restart and now - child.stop_sent > STOP_GRACE_S:
                    child.process.kill()  # it ignored the polite request
                continue
            if child.process is not None:  # it exited
                code = child.process.returncode
                ran = now - child.started
                child.process = None
                if child.restart:
                    log.info("%s stopped to cut its connections", child.name)
                else:
                    child.failures = 0 if ran >= STEADY_S else child.failures + 1
                    child.not_before = now + backoff(child.failures)
                    log.warning("%s exited with %s after %.0fs; next start in %.0fs",
                                child.name, code, ran, child.not_before - now)
                    if child.name == "xvfb" and service.running():
                        service.process.terminate()  # its browser lost the screen
            if child.restart:  # asked for, so no waiting, whether it was running or not
                child.restart, child.failures, child.not_before = False, 0, now
            if now < child.not_before or (child.needs_screen and not screen_up):
                continue
            if child.name == "xvfb":
                for stale in (Path(f"/tmp/.X{display.lstrip(':')}-lock"), screen_socket(display)):  # noqa: S108
                    try:
                        stale.unlink()
                    except OSError:
                        pass
            if child is screen:
                pair = vnc.passwords()
                if pair is None:
                    child.failures += 1
                    child.not_before = now + backoff(child.failures)
                    if child.failures == 1:  # said once, then tried again quietly
                        log.error("the screen stays off: the VNC password files are missing, unusable "
                                  "or start with the same 8 characters (make secrets creates them)")
                    continue
                vnc.write_passwd_file(*pair)
                del pair
            # Only the service needs our settings (the token's file name among them).
            if child is service:
                env = service_env
            elif child is screen:
                env = vnc.environment()
            else:
                env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/tmp"}  # noqa: S108
            child.process = subprocess.Popen(child.argv, env=env)  # noqa: S603 -- fixed commands, no shell
            child.started = now
            if child is screen:
                vnc.record_pid(child.process.pid)
            log.info("started %s", child.name)
        time.sleep(0.1)

    # Service first, so Chromium can write cookies to disk before its screen goes away.
    for child in reversed(children):
        if child.running():
            child.process.terminate()
            try:
                child.process.wait(timeout=25)
            except subprocess.TimeoutExpired:
                child.process.kill()
    return 0
