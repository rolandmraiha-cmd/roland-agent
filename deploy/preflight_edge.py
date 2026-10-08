"""Read-only checks of the resolved Compose configuration; no deployment or firewall changes.

Covers caddy, core, model and sandbox, the browser (M6), screen relay (M7) and optional
trainer (M8) when their profiles are on.
"""

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
    known = {"caddy", "core", "model", "sandbox", "browser", "novnc", "trainer"}
    if not {"caddy", "core", "model", "sandbox"} <= set(services) <= known:
        return ["The stack must contain caddy, core, model and sandbox, and may add only browser, novnc and trainer"]
    trainer = services.get("trainer")
    trainer_url = services["core"].get("environment", {}).get("TRAINER_URL", "")
    if bool(trainer) != bool(trainer_url):
        errors.append("COMPOSE_PROFILES=training and TRAINER_URL must be enabled together")
    if trainer:
        if (trainer_url != "http://10.77.7.70:7200" or trainer.get("ports")
                or set(trainer.get("networks", {})) != {"trainer_ctl", "trainer_egress"}):
            errors.append("Trainer must use only its private control address and egress network")
        mounts = {mount.get("target"): mount for mount in trainer.get("volumes", [])}
        if (not mounts.get("/training-data", {}).get("read_only") or "/models" not in mounts
                or mounts["/models"].get("read_only") or "/training-runs" not in mounts
                or any(path in mounts for path in ("/data", "/workspace", "/profile"))):
            errors.append("Trainer may read training data and write only its runs and model registry")
        if config.get("networks", {}).get("trainer_ctl", {}).get("internal") is not True:
            errors.append("Trainer control network must be internal")
        if any(trainer.get("environment", {}).get(name) for name in SECRET_ENV):
            errors.append("Use mounted trainer secrets")
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
    model = services["model"]
    if model.get("ports") or set(model.get("networks", {})) != {"model"}:
        errors.append("Model must have only its internal network and no published ports")
    if config.get("networks", {}).get("model", {}).get("internal") is not True:
        errors.append("Model network must be internal")
    model_volumes = model.get("volumes", [])
    if not any(mount.get("target") == "/models" and mount.get("read_only") for mount in model_volumes):
        errors.append("Model weights must be mounted read-only")
    try:
        model_env = model.get("environment", {})
        if (
            not 1 <= int(model_env.get("MODEL_CTX", 0)) <= 6144
            or not 1 <= int(model_env.get("MODEL_THREADS", 0)) <= 3
        ):
            errors.append("Model context or thread count is outside the current host limits")
    except (ValueError, TypeError):
        errors.append("Invalid model context or thread count")
    sandbox = services["sandbox"]
    if sandbox.get("ports") or set(sandbox.get("networks", {})) != {"sandbox_ctl", "sandbox_egress"}:
        errors.append("Sandbox must have only sandbox_ctl and sandbox_egress networks and no published ports")
    if config.get("networks", {}).get("sandbox_ctl", {}).get("internal") is not True:
        errors.append("Sandbox control network must be internal")
    sandbox_env = sandbox.get("environment", {})
    if (
        sandbox_env.get("SANDBOXD_HOST") != "10.77.3.20"
        or sandbox_env.get("SANDBOXD_ALLOWED_PEERS") != "10.77.3.10"
    ):
        errors.append("Sandbox must listen on the control address and allow only core")
    if any(sandbox_env.get(name) for name in SECRET_ENV):
        errors.append("Remove direct secret values from sandbox environment; use mounted secret files")
    core = services["core"].get("environment", {})
    caddy = services["caddy"].get("environment", {})
    errors.extend(browser_errors(config))
    errors.extend(screen_errors(config))
    for key, value in {
        "AGENT_ENV": "production",
        "COOKIE_SECURE": "true",
        "HOST": "10.77.1.10",
        "PORT": "8080",
        "FORWARDED_ALLOW_IPS": "10.77.1.2",
        "CORE_ALLOWED_PEERS": "10.77.1.2",
        "ALLOW_SHELL": "true",
        "SHELL_BACKEND": "sandbox",
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


def browser_errors(config: dict) -> list[str]:
    """The browser is off unless both switches are on: the Compose profile that starts the
    service, and BROWSER_ENABLED for core. Checks the service's shape when it is there."""
    errors = []
    services = config.get("services", {})
    core = services["core"]
    enabled = str(core.get("environment", {}).get("BROWSER_ENABLED", "")).lower()
    browser = services.get("browser")
    if enabled not in {"true", "false"}:
        errors.append("Core BROWSER_ENABLED must be true or false")
    if core.get("environment", {}).get("BROWSER_URL") != "http://10.77.4.40:7100":
        errors.append("Core must reach the browser at its control address")
    for name, service in services.items():
        mounts = service.get("volumes", [])
        if name != "browser" and any(
            mount.get("source") == "browser-profile" or mount.get("target") == "/profile" for mount in mounts
        ):
            errors.append(f"{name} must not mount the browser profile")
    if browser is None:
        if enabled == "true":
            errors.append("BROWSER_ENABLED=true needs the browser service: set COMPOSE_PROFILES=browser in .env")
        return errors
    if browser.get("ports") or set(browser.get("networks", {})) != {"browser_ctl", "browser_egress", "vnc"}:
        errors.append("Browser must have only browser_ctl, browser_egress and vnc networks and no published ports")
    if config.get("networks", {}).get("browser_ctl", {}).get("internal") is not True:
        errors.append("Browser control network must be internal")
    env = browser.get("environment", {})
    if env.get("BROWSERD_HOST") != "10.77.4.40" or env.get("BROWSERD_ALLOWED_PEERS") != "10.77.4.10":
        errors.append("Browser must listen on the control address and allow only core")
    if env.get("BROWSER_ALLOW_PRIVATE_HOSTS"):
        errors.append("BROWSER_ALLOW_PRIVATE_HOSTS is for tests only and must not be set here")
    if any(env.get(name) for name in SECRET_ENV):
        errors.append("Remove direct secret values from browser environment; use mounted secret files")
    mounts = {mount.get("target"): mount for mount in browser.get("volumes", [])}
    workspace = next(
        (mount.get("source") for mount in core.get("volumes", []) if mount.get("target") == "/workspace"), None
    )
    if set(mounts) != {"/profile", "/files"} or mounts["/profile"].get("source") != "browser-profile":
        errors.append("Browser must mount only its profile volume and its files folder")
    elif not workspace or mounts["/files"].get("source") != f"{str(workspace).rstrip('/')}/browser":
        errors.append("Browser files must be the browser folder of the workspace")
    seccomp = [option for option in browser.get("security_opt", []) if option.startswith("seccomp")]
    own_profile = ROOT / "docker/browser/seccomp-chromium.json"
    custom = False
    for option in seccomp:
        value = option.partition("=")[2] or option.partition(":")[2]
        if value == "builtin":
            continue
        custom = True
        try:
            if (ROOT / value).resolve() != own_profile.resolve():
                raise ValueError
        except (OSError, ValueError):
            errors.append("Browser seccomp profile must be Docker's own or docker/browser/seccomp-chromium.json")
    if str(env.get("BROWSER_CHROMIUM_SANDBOX", "false")).lower() == "true" and not custom:
        errors.append(
            "BROWSER_CHROMIUM_SANDBOX=true also needs BROWSER_SECCOMP=./docker/browser/seccomp-chromium.json"
        )
    return errors


NOVNC_COMMAND = ["websockify", "--web", "/opt/novnc", "--file-only", "--heartbeat=30", "10.77.2.30:6080", "10.77.5.40:5900"]
VNC_SECRETS = {"vnc_password", "vnc_view_password"}


def _address(service: dict, network: str) -> str | None:
    return (service.get("networks", {}).get(network) or {}).get("ipv4_address")


def _secret_names(service: dict) -> set[str]:
    return {item.get("source") for item in service.get("secrets", []) if isinstance(item, dict)}


def screen_errors(config: dict) -> list[str]:
    """The screen (M7) is off unless three things agree: SCREEN_ENABLED for core and the
    browser, the browser itself, and the Compose profile that starts the noVNC relay.
    Checks the relay's shape when it is there: two internal networks, nothing else."""
    errors = []
    services = config.get("services", {})
    networks = config.get("networks", {})
    core_env = services["core"].get("environment", {})
    enabled = str(core_env.get("SCREEN_ENABLED", "")).lower()
    browser = services.get("browser")
    novnc = services.get("novnc")
    if enabled not in {"true", "false"}:
        errors.append("Core SCREEN_ENABLED must be true or false")
    for name in ("screen", "vnc"):
        # Compose leaves a network out of the resolved file while no running service uses it.
        if name in networks and networks[name].get("internal") is not True:
            errors.append(f"The {name} network must be internal")
    # Only core (hands them to Roland's page) and the browser (x11vnc checks them) hold them.
    for name, service in services.items():
        if name not in {"core", "browser"} and _secret_names(service) & VNC_SECRETS:
            errors.append(f"{name} must not be given the screen passwords")
    if _address(services["caddy"], "screen") != "10.77.2.2":
        errors.append("Caddy must be on the screen network at its fixed address")
    if set(services["core"].get("networks", {})) & {"screen", "vnc"}:
        errors.append("Core must not be on the screen or vnc network: the screen never passes through it")
    if browser is not None:
        env = browser.get("environment", {})
        if str(env.get("SCREEN_ENABLED", "")).lower() != enabled:
            errors.append("Core and the browser must agree on SCREEN_ENABLED")
        if (
            env.get("VNC_LISTEN") != "10.77.5.40"
            or env.get("VNC_ALLOWED_PEERS") != "10.77.5.30"
            or _address(browser, "vnc") != "10.77.5.40"
        ):
            errors.append("The screen server must listen on the browser's vnc address and allow only noVNC")
    if enabled == "true":
        if browser is None or str(core_env.get("BROWSER_ENABLED", "")).lower() != "true":
            errors.append("SCREEN_ENABLED=true needs the browser: BROWSER_ENABLED=true and the browser profile")
        if novnc is None:
            errors.append(
                "SCREEN_ENABLED=true needs the noVNC service: set COMPOSE_PROFILES=browser,screen in .env"
            )
    if novnc is None:
        return errors
    if (
        novnc.get("ports")
        or set(novnc.get("networks", {})) != {"screen", "vnc"}
        or _address(novnc, "screen") != "10.77.2.30"
        or _address(novnc, "vnc") != "10.77.5.30"
    ):
        errors.append("noVNC must have only the screen and vnc networks, at its fixed addresses, and no published ports")
    if novnc.get("command") not in (None, NOVNC_COMMAND) or novnc.get("entrypoint"):
        errors.append("noVNC must run the image's own websockify command (no token, certificate or traffic options)")
    if novnc.get("secrets") or novnc.get("env_file") or any(novnc.get("environment", {}).get(name) for name in SECRET_ENV):
        errors.append("noVNC must hold no secret")
    if any(mount.get("type") != "tmpfs" for mount in novnc.get("volumes", [])):
        errors.append("noVNC must mount nothing")
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
    # Every secret a container mounts. Compose can't start a service whose secret file is
    # missing, so a missing one must stop a deploy here, before anything is recreated.
    required = {
        "agent_password_hash", "model_server_token", "sandbox_api_token", "browser_api_token",
        "vnc_password", "vnc_view_password", "trainer_api_token",
    } | {
        secret["source"] for service in config["services"].values()
        for secret in service.get("secrets", []) if isinstance(secret, dict) and secret.get("source")
    }
    for name in sorted(required):
        # Core mounts the two screen passwords since M7, whether or not the screen is on.
        # `make secrets` creates any that are missing and never changes one that exists.
        hint = " (run: sudo env APPLY=1 make secrets)" if name in VNC_SECRETS else ""
        filename = secrets.get(name, {}).get("file")
        if not filename:
            errors.append(f"{name}: required file secret is missing{hint}")
            continue
        path = Path(filename)
        errors.extend(f"{name}: {error}{hint}" for error in private_path_errors(path, directory=False))
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
        command = ["docker", "compose", "-f", str(ROOT / "docker-compose.yml")]
        # Compose overrides for explicitly configured credentials are checked as well.
        import os
        mode = os.environ.get("TRAINING_COMPOSE_OVERRIDE", "")
        if mode in {"ssh", "hook"}:
            command += ["-f", str(ROOT / ("docker-compose.training-" + mode + ".yml"))]
        output = subprocess.check_output(
            command + ["config", "--format", "json"],
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
    print(
        "Edge/runtime policy checks passed. Model-file, firewall and workspace-quota preflight remains pending."
    )
    if "browser" in config.get("services", {}):
        print("The browser service is part of this stack (COMPOSE_PROFILES=browser).")
    if "novnc" in config.get("services", {}):
        screen_on = str(config["services"]["core"].get("environment", {}).get("SCREEN_ENABLED", "")).lower() == "true"
        print(
            "The screen relay (novnc) is part of this stack (COMPOSE_PROFILES has screen); "
            + ("the screen is switched on." if screen_on else "SCREEN_ENABLED is false, so it is not used.")
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
