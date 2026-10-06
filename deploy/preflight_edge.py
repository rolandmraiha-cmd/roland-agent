"""Read-only checks for the M2 edge slice; no deployment or firewall changes."""

from __future__ import annotations

import json
import re
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRET_ENV = (
    "AGENT_PASSWORD_HASH",
    "MODEL_SERVER_TOKEN",
    "SANDBOX_API_TOKEN",
    "BROWSER_API_TOKEN",
    "VNC_PASSWORD",
    "VNC_VIEW_PASSWORD",
    "TRAINER_API_TOKEN",
)


def version(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", value.strip())
    if not match:
        raise ValueError("Cannot parse Docker/Compose version")
    return tuple(int(part) for part in match.groups())


def configuration_errors(config: dict) -> list[str]:
    """Inspect resolved Compose JSON without printing environment values."""
    errors = []
    services = config.get("services", {})
    if set(services) != {"caddy", "core"}:
        return ["This edge slice must contain only caddy and core"]
    for name, service in services.items():
        if (
            service.get("user") != "1000:1000"
            or service.get("read_only") is not True
            or service.get("cap_drop") != ["ALL"]
            or "no-new-privileges:true" not in service.get("security_opt", [])
            or service.get("restart") != "unless-stopped"
            or not service.get("healthcheck")
        ):
            errors.append(f"{name} is missing required runtime hardening")
        if any(service.get(key) for key in ("privileged", "network_mode", "pid", "ipc", "cap_add")):
            errors.append(f"{name} has a forbidden container option")
        if any("unconfined" in option for option in service.get("security_opt", [])):
            errors.append(f"{name} disables a required security profile")
        if any(volume.get("source") == "/var/run/docker.sock" for volume in service.get("volumes", [])):
            errors.append(f"{name} mounts the Docker socket")
        try:
            limits_valid = all(
                float(service.get(key, 0)) > 0 for key in ("mem_limit", "memswap_limit", "cpus", "pids_limit")
            )
        except (TypeError, ValueError):
            limits_valid = False
        if not limits_valid:
            errors.append(f"{name} is missing resource limits")
    ports = {
        (str(port.get("published")), port.get("target"), port.get("protocol", "tcp"))
        for port in services["caddy"].get("ports", [])
    }
    if ports != {("80", 80, "tcp"), ("443", 443, "tcp"), ("443", 443, "udp")}:
        errors.append("Caddy must publish only 80/tcp, 443/tcp and 443/udp")
    if services["core"].get("ports"):
        errors.append("Core must not publish a port")
    core = services["core"].get("environment", {})
    caddy = services["caddy"].get("environment", {})
    for key, value in {
        "AGENT_ENV": "production",
        "COOKIE_SECURE": "true",
        "HOST": "10.77.1.10",
        "PORT": "8080",
        "FORWARDED_ALLOW_IPS": "10.77.1.2",
        "CORE_ALLOWED_PEERS": "10.77.1.2",
        "ALLOW_SHELL": "false",
        "BROWSER_ENABLED": "false",
        "SCREEN_ENABLED": "false",
        "TRAINING_CAPTURE": "false",
        "TRAINING_LOOP_ENABLED": "false",
    }.items():
        if core.get(key) != value:
            errors.append(f"Core {key} does not match the current milestone")
    host = core.get("AGENT_HOST", "")
    labels = host.split(".")
    if (
        not host
        or len(host) > 253
        or any(
            not 1 <= len(label) <= 63
            or not label[0].isascii()
            or not label[0].isalnum()
            or not label[-1].isascii()
            or not label[-1].isalnum()
            or any(not (char.isascii() and (char.isalnum() or char == "-")) for char in label)
            for label in labels
        )
    ):
        errors.append("AGENT_HOST must be a plain DNS hostname or IPv4 address")
    if caddy.get("AGENT_HOST") != host or core.get("ALLOWED_HOSTS") != host:
        errors.append("Caddy, core and ALLOWED_HOSTS must share the computed hostname")
    if caddy.get("CADDY_TLS") not in {"acme", "internal"}:
        errors.append("CADDY_TLS must be acme or internal")
    email = caddy.get("ACME_EMAIL", "")
    if any(not 0x21 <= ord(char) <= 0x7E or char in '{}"\\' for char in email):
        errors.append("ACME_EMAIL contains invalid configuration characters")
    try:
        if int(caddy.get("UPLOAD_MAX_MB", "")) < 1:
            raise ValueError
    except (ValueError, TypeError):
        errors.append("UPLOAD_MAX_MB must be a positive integer")
    if any(core.get(name) for name in SECRET_ENV):
        errors.append("Remove direct secret values from .env; use the mounted secret files")
    return errors


def private_path_errors(path: Path, *, directory: bool, uid: int = 1000) -> list[str]:
    """Check metadata only. Never read or print a secret value."""
    try:
        info = path.lstat()
        if path.absolute() != path.resolve(strict=True):
            return ["Private paths must not contain symlink or traversal components"]
    except OSError:
        return ["Required private path is missing or inaccessible"]
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    expected_mode = 0o700 if directory else 0o400
    if not expected_type(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) != expected_mode:
        return [
            f"Private {'directory' if directory else 'file'} must be owned by uid {uid}, mode {expected_mode:04o}, and not a symlink"
        ]
    if not directory and info.st_size == 0:
        return ["Required secret file is empty"]
    return []


def path_errors(config: dict) -> list[str]:
    errors = []
    secrets = config.get("secrets", {})
    for name in ("agent_password_hash", "model_server_token"):
        filename = secrets.get(name, {}).get("file")
        if not filename:
            errors.append(f"{name}: required file secret is missing")
            continue
        path = Path(filename)
        errors.extend(f"{name}: {error}" for error in private_path_errors(path, directory=False))
        errors.extend(f"{name} parent: {error}" for error in private_path_errors(path.parent, directory=True))
    mounts = config["services"]["core"].get("volumes", [])
    workspace = next((mount.get("source") for mount in mounts if mount.get("target") == "/workspace"), None)
    if not workspace or not Path(workspace).is_absolute() or Path(workspace) == Path("/"):
        errors.append("Workspace must be a dedicated absolute directory")
    else:
        errors.extend(f"workspace: {error}" for error in private_path_errors(Path(workspace), directory=True))
    return errors


def main() -> int:
    try:
        engine = subprocess.check_output(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            text=True,
            stderr=subprocess.PIPE,
            timeout=15,
        )
        compose = subprocess.check_output(
            ["docker", "compose", "version", "--short"],
            text=True,
            stderr=subprocess.PIPE,
            timeout=15,
        )
        if version(engine) < (28, 0, 0) or version(compose) < (2, 33, 1):
            raise ValueError("Require Docker Engine >= 28 and Compose >= 2.33.1 for gateway priority")
        output = subprocess.check_output(
            ["docker", "compose", "-f", str(ROOT / "docker-compose.yml"), "config", "--format", "json"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        config = json.loads(output)
        errors = configuration_errors(config)
        if not errors:
            errors.extend(path_errors(config))
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        message = (
            str(error)
            if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError)
            else "Docker/Compose or its resolved configuration is unavailable"
        )
        print(message, file=sys.stderr)
        return 1
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        return 1
    print("Edge checks passed. Full model, firewall and workspace-quota preflight remains pending.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
