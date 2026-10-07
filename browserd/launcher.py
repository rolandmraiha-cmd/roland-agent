"""Starts and looks after browserd's processes (§6.5): the virtual screen and the service.

A process that exits is started again, waiting longer each time up to 30 seconds. In M7 the
screen-sharing server (x11vnc) joins the list in `extra_children`.
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

from .settings import _bool, _viewport

log = logging.getLogger("browserd.launcher")
MAX_BACKOFF_S = 30
STEADY_S = 60  # a process that ran this long starts again without delay


@dataclass
class Child:
    name: str
    argv: list[str]
    needs_screen: bool = False
    process: subprocess.Popen | None = None
    failures: int = 0
    not_before: float = 0.0
    started: float = 0.0

    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None


def screen_socket(display: str) -> Path:
    return Path(f"/tmp/.X11-unix/X{display.lstrip(':')}")  # noqa: S108 -- where X servers listen


def xvfb_child(display: str, width: int, height: int) -> Child:
    return Child("xvfb", [
        "Xvfb", display, "-screen", "0", f"{width}x{height}x24", "-nolisten", "tcp", "-noreset",
    ])


def extra_children(display: str) -> list[Child]:
    """Hook for M7: x11vnc on the `vnc` network goes here, with needs_screen=True."""
    return []


def backoff(failures: int) -> float:
    return float(min(MAX_BACKOFF_S, 2 ** min(failures, 6))) if failures else 0.0


def run() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    display = os.environ.get("DISPLAY", "").strip() or ":99"
    headless = _bool("BROWSERD_HEADLESS")
    width, height = _viewport()
    service = Child("browserd", [sys.executable, "-m", "browserd", "serve"], needs_screen=not headless)
    children = ([] if headless else [xvfb_child(display, width, height), *extra_children(display)]) + [service]
    stopping = []

    def stop(_signum, _frame) -> None:
        stopping.append(True)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while not stopping:
        now = time.monotonic()
        screen_up = headless or (children[0].running() and screen_socket(display).exists())
        for child in children:
            if child.running():
                continue
            if child.process is not None:  # it exited
                code = child.process.returncode
                ran = now - child.started
                child.failures = 0 if ran >= STEADY_S else child.failures + 1
                child.not_before = now + backoff(child.failures)
                child.process = None
                log.warning("%s exited with %s after %.0fs; next start in %.0fs",
                            child.name, code, ran, child.not_before - now)
                if child.name == "xvfb" and service.running():
                    service.process.terminate()  # its browser lost the screen
            if now < child.not_before or (child.needs_screen and not screen_up):
                continue
            if child.name == "xvfb":
                for stale in (Path(f"/tmp/.X{display.lstrip(':')}-lock"), screen_socket(display)):  # noqa: S108
                    try:
                        stale.unlink()
                    except OSError:
                        pass
            child.process = subprocess.Popen(child.argv)  # noqa: S603 -- fixed commands, no shell
            child.started = now
            log.info("started %s", child.name)
        time.sleep(0.25)

    # Service first, so Chromium can write cookies to disk before its screen goes away.
    for child in reversed(children):
        if child.running():
            child.process.terminate()
            try:
                child.process.wait(timeout=25)
            except subprocess.TimeoutExpired:
                child.process.kill()
    return 0
