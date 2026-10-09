"""Validate Docker observations without treating missing data as a pass."""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path


def expected_services(env_file: Path) -> set[str]:
    """Read feature switches as data; never source a deployment env file."""
    values = {}
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip().removeprefix("export ")
            key, separator, raw = line.partition("=")
            if separator and key.strip() in {"BROWSER_ENABLED", "SCREEN_ENABLED", "TRAINER_URL"}:
                parts = shlex.split(raw, comments=True)
                values[key.strip()] = parts[0] if len(parts) == 1 else ""
    required = {"caddy", "core", "model", "sandbox"}
    for key, name in (("BROWSER_ENABLED", "browser"), ("SCREEN_ENABLED", "novnc")):
        if os.environ.get(key, values.get(key, "")).lower() in {"1", "true", "yes", "on"}:
            required.add(name)
    if os.environ.get("TRAINER_URL", values.get("TRAINER_URL", "")).strip():
        required.add("trainer")
    return required


def services(text: str, required: set[str] | None = None) -> None:
    """Accept Compose's JSON array or one JSON object per line."""
    try:
        rows = json.loads(text)
    except json.JSONDecodeError:
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list) or not rows:
        raise ValueError("no Compose containers found")
    found = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("Service"), str):
            raise ValueError("invalid Compose service metadata")
        name = row["Service"]
        found.add(name)
        if row.get("State") != "running" or row.get("Health") != "healthy":
            raise ValueError(f"service not running and healthy: {name}")
        # A loopback publication still exposes an internal service to the host.
        publishers = row.get("Publishers") or []
        if not isinstance(publishers, list):
            raise ValueError("invalid port metadata")
        published = any(
            not isinstance(port, dict) or port.get("PublishedPort", 0) != 0 for port in publishers
        )
        if name != "caddy" and (published or "->" in str(row.get("Ports", ""))):
            raise ValueError(f"non-caddy published ports: {name}")
    missing = (required or {"caddy", "core", "model", "sandbox"}) - found
    if missing:
        raise ValueError("missing services: " + ", ".join(sorted(missing)))


def logs(text: str) -> None:
    config = json.loads(text)
    if not isinstance(config, dict) or config.get("Type") != "json-file":
        raise ValueError("log driver must be json-file")
    options = config.get("Config")
    if not isinstance(options, dict) or options.get("max-size") != "10m" or options.get("max-file") != "3":
        raise ValueError("log rotation must be max-size=10m and max-file=3")


def main() -> None:
    if len(sys.argv) not in {2, 3} or sys.argv[1] not in {"services", "logs"}:
        raise SystemExit("Usage: verify_state.py services [env-file]|logs")
    try:
        text = sys.stdin.read(1024 * 1024 + 1)
        if len(text) > 1024 * 1024:
            raise ValueError("Docker observation too large")
        if sys.argv[1] == "services":
            required = expected_services(Path(sys.argv[2])) if len(sys.argv) == 3 else None
            services(text, required)
        else:
            logs(text)
    except (ValueError, TypeError, OSError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
