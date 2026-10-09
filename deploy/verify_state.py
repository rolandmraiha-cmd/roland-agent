"""Validate Docker observations without treating missing data as a pass."""

from __future__ import annotations

import json
import sys


def expected_services(config: dict) -> set[str]:
    """Use Compose's resolved environment, including interpolation and overrides."""
    try:
        values = config["services"]["core"]["environment"]
    except (KeyError, TypeError) as error:
        raise ValueError("missing resolved core configuration") from error
    if not isinstance(values, dict):
        raise ValueError("invalid resolved core environment")
    required = {"caddy", "core", "model", "sandbox"}
    for key, name in (("BROWSER_ENABLED", "browser"), ("SCREEN_ENABLED", "novnc")):
        value = values.get(key, "false")
        if isinstance(value, bool):
            enabled = value
        elif isinstance(value, str) and value.strip().lower() in {
            "true",
            "1",
            "yes",
            "on",
            "false",
            "0",
            "no",
            "off",
        }:
            enabled = value.strip().lower() in {"true", "1", "yes", "on"}
        else:
            raise ValueError("invalid resolved feature switch")
        if enabled:
            required.add(name)
    if values.get("TRAINER_URL"):
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
    if len(sys.argv) not in {2, 3} or sys.argv[1] not in {"services", "logs", "expected"}:
        raise SystemExit("Usage: verify_state.py expected|services [required-json]|logs")
    try:
        text = sys.stdin.read(1024 * 1024 + 1)
        if len(text) > 1024 * 1024:
            raise ValueError("Docker observation too large")
        if sys.argv[1] == "services":
            required = None
            if len(sys.argv) == 3:
                names = json.loads(sys.argv[2])
                if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
                    raise ValueError("invalid required services")
                required = set(names)
            services(text, required)
        elif sys.argv[1] == "expected":
            print(json.dumps(sorted(expected_services(json.loads(text)))))
        else:
            logs(text)
    except (ValueError, TypeError, OSError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
