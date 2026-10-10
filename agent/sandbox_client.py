"""SandboxShell: HTTP client for sandboxd (§6.2.5 / §8.3)."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from .local_shell import ShellResult


@dataclass
class SandboxShell:
    url: str
    token: str
    timeout_default: int = 60
    timeout_max: int = 300

    async def run(self, command: str, timeout_s: int) -> ShellResult:
        timeout_s = max(1, min(int(timeout_s), self.timeout_max))
        headers = {"Authorization": f"Bearer {self.token}"}
        payload = {"command": command, "timeout_s": timeout_s, "cwd": "."}
        async with httpx.AsyncClient(
            trust_env=False,
            timeout=timeout_s + 10,
        ) as client:
            response = await client.post(
                f"{self.url.rstrip('/')}/v1/exec",
                json=payload,
                headers=headers,
            )
        if response.status_code == 429:
            return ShellResult(
                exit_code=-1,
                output="Error: sandbox is busy (too many concurrent commands).",
                truncated=False,
                timed_out=False,
                duration_ms=0,
            )
        if response.status_code >= 400:
            detail = response.text[:500]
            return ShellResult(
                exit_code=-1,
                output=f"Error: sandbox rejected the command (HTTP {response.status_code}): {detail}",
                truncated=False,
                timed_out=False,
                duration_ms=0,
            )
        data = response.json()
        return ShellResult(
            exit_code=int(data.get("exit_code", -1)),
            output=str(data.get("output", "")),
            truncated=bool(data.get("truncated")),
            timed_out=bool(data.get("timed_out")),
            duration_ms=int(data.get("duration_ms", 0)),
        )

    async def healthy(self) -> bool:
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
                response = await client.get(f"{self.url.rstrip('/')}/healthz")
            return response.status_code == 200 and response.json() == {"ok": True}
        except (httpx.HTTPError, ValueError):
            return False
