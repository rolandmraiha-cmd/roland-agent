"""LocalShell: v1 run_shell subprocess backend for tests and development (§6.2.5)."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from pathlib import Path

# Environment variables the shell never gets, so commands can't print the agent's secrets.
SECRET_ENV = {"AGENT_PASSWORD_HASH", "MODEL_API_KEY", "MODEL_SERVER_TOKEN"}
# Match tools.MAX_OUTPUT so LocalShell caps stay identical to v1.
MAX_OUTPUT = 8000


@dataclass
class ShellResult:
    exit_code: int
    output: str
    truncated: bool
    timed_out: bool
    duration_ms: int


class LocalShell:
    """Runs commands in-process with the v1 subprocess semantics."""

    def __init__(self, workspace: Path, *, max_output: int = MAX_OUTPUT):
        self.workspace = workspace
        self.max_output = max_output

    async def run(self, command: str, timeout_s: int) -> ShellResult:
        self.workspace.mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
        started = time.monotonic()
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=self.workspace,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )

        def kill() -> None:
            try:
                os.killpg(proc.pid, 9)
            except ProcessLookupError:
                pass

        async def read_capped() -> tuple[bytes, bool]:
            out = b""
            assert proc.stdout is not None
            while len(out) <= self.max_output:
                part = await proc.stdout.read(4096)
                if not part:
                    return out, False
                out += part
            kill()
            return out, True

        async def finish() -> None:
            try:
                await asyncio.wait_for(proc.communicate(), timeout=5)
            except TimeoutError:
                pass

        truncated = False
        timed_out = False
        try:
            out, truncated = await asyncio.wait_for(read_capped(), timeout=timeout_s)
            await finish()
        except asyncio.CancelledError:
            kill()
            await finish()
            raise
        except TimeoutError:
            timed_out = True
            kill()
            await finish()
            out = b""
        duration_ms = int((time.monotonic() - started) * 1000)
        text = out.decode(errors="replace")
        exit_code = -1 if timed_out else (proc.returncode if proc.returncode is not None else -1)
        return ShellResult(
            exit_code=exit_code,
            output=text,
            truncated=truncated,
            timed_out=timed_out,
            duration_ms=duration_ms,
        )
