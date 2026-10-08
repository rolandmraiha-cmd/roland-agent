"""Container policy and resolved-host checks: caddy, core, model, sandbox and (M6) browser."""

import copy
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
spec = importlib.util.spec_from_file_location("preflight_edge", ROOT / "deploy/preflight_edge.py")
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


def compose_command():
    if binary := os.getenv("COMPOSE_BIN"):
        return [binary]
    if docker := shutil.which("docker"):
        return [docker, "compose"]
    pytest.skip("Docker Compose CLI is needed for resolved-config checks (no daemon required)")


def resolve(folder: Path, extra_env: str = "", files: tuple[str, ...] = ("docker-compose.yml",)) -> dict:
    """What Compose makes of the files with .env.example plus `extra_env` as the .env."""
    (folder / ".env").write_text((ROOT / ".env.example").read_text() + extra_env)
    command = compose_command()
    for name in files:
        command += ["-f", str(ROOT / name)]
    result = subprocess.run(
        command + ["--project-directory", str(folder), "config", "--format", "json"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        env={
            key: value
            for key, value in os.environ.items()
            if key
            not in {"AGENT_DOMAIN", "AGENT_FALLBACK_HOST", "CADDY_TLS", "COMPOSE_PROFILES", "COMPOSE_FILE"}
            and not key.startswith("BROWSER_")
        },
    )
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def resolved(tmp_path_factory):
    return resolve(tmp_path_factory.mktemp("edge-compose"))


@pytest.fixture(scope="module")
def resolved_with_browser(tmp_path_factory):
    """The stack as it is on the server once Roland switches the browser on."""
    return resolve(
        tmp_path_factory.mktemp("browser-compose"), "\nCOMPOSE_PROFILES=browser\nBROWSER_ENABLED=true\n"
    )


def test_only_caddy_publishes_expected_ports():
    assert set(COMPOSE["services"]) == {"core", "caddy", "model", "sandbox", "browser"}
    assert COMPOSE["services"]["caddy"]["ports"] == ["80:80/tcp", "443:443/tcp", "443:443/udp"]
    for name in ("core", "model", "sandbox", "browser"):
        assert "ports" not in COMPOSE["services"][name] and "expose" not in COMPOSE["services"][name]
    assert COMPOSE["name"] == "roland-agent"


@pytest.mark.parametrize("name", ["core", "caddy", "model", "sandbox"])
def test_every_service_has_security_and_resource_limits(name):
    service = COMPOSE["services"][name]
    assert service["user"] == "1000:1000"
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["restart"] == "unless-stopped"
    assert service["healthcheck"]["test"][0] == "CMD"
    assert service["memswap_limit"] == service["mem_limit"]
    assert service["mem_limit"] and service["cpus"] and service["pids_limit"] > 0
    assert service["logging"] == {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}}
    assert not set(service) & {"privileged", "network_mode", "pid", "ipc", "cap_add"}
    assert "unconfined" not in json.dumps(service)
    assert "docker.sock" not in json.dumps(service)


def test_reserved_internal_bridges_and_current_egress_members():
    internal = {"edge", "screen", "sandbox_ctl", "browser_ctl", "vnc", "model", "trainer_ctl"}
    expected = {
        "public": 0,
        "edge": 1,
        "screen": 2,
        "sandbox_ctl": 3,
        "browser_ctl": 4,
        "vnc": 5,
        "model": 6,
        "trainer_ctl": 7,
        "core_egress": 10,
        "sandbox_egress": 11,
        "browser_egress": 12,
        "trainer_egress": 13,
    }
    assert set(COMPOSE["networks"]) == set(expected)
    for name, suffix in expected.items():
        network = COMPOSE["networks"][name]
        assert bool(network.get("internal")) == (name in internal)
        assert network["enable_ipv6"] is False
        assert network["ipam"]["config"] == [{"subnet": f"10.77.{suffix}.0/24"}]
    caddy = COMPOSE["services"]["caddy"]["networks"]
    core = COMPOSE["services"]["core"]["networks"]
    assert set(caddy) == {"public", "edge", "screen"}
    assert set(core) == {"edge", "sandbox_ctl", "browser_ctl", "model", "core_egress"}
    assert core["sandbox_ctl"]["ipv4_address"] == "10.77.3.10"
    assert core["browser_ctl"]["ipv4_address"] == "10.77.4.10"
    assert caddy["edge"]["ipv4_address"] == "10.77.1.2"
    assert core["edge"]["ipv4_address"] == "10.77.1.10"
    assert core["model"]["ipv4_address"] == "10.77.6.10"
    assert core["core_egress"]["gw_priority"] == 100


def test_unimplemented_features_cannot_be_enabled_by_env_file():
    core = COMPOSE["services"]["core"]
    env = core["environment"]
    # Shell is on via sandbox in M4. The browser is off unless .env turns it on (M6);
    # screen and training can't be turned on from .env at all.
    assert env["ALLOW_SHELL"] == "true"
    assert env["SHELL_BACKEND"] == "sandbox"
    assert env["BROWSER_ENABLED"] == "${BROWSER_ENABLED:-false}"
    assert env["BROWSER_URL"] == "http://10.77.4.40:7100"
    assert env["BROWSER_API_TOKEN_FILE"] == "/run/secrets/browser_api_token"
    for flag in (
        "SCREEN_ENABLED",
        "TRAINING_CAPTURE",
        "TRAINING_LOOP_ENABLED",
    ):
        assert env[flag] == "false"
    assert env["HOST"] == "10.77.1.10"
    assert env["AGENT_ENV"] == "production" and env["COOKIE_SECURE"] == "true"
    assert env["CORE_ALLOWED_PEERS"] == env["FORWARDED_ALLOW_IPS"] == "10.77.1.2"
    assert core["secrets"] == ["model_server_token", "agent_password_hash", "sandbox_api_token", "browser_api_token"]
    assert "env_file" not in COMPOSE["services"]["caddy"]
    assert core["env_file"] == ".env"
    assert env["AGENT_PASSWORD_HASH_FILE"] == "/run/secrets/agent_password_hash"
    assert env["MODEL_SERVER_TOKEN_FILE"] == "/run/secrets/model_server_token"


def test_persistent_private_mounts_and_existing_data_volume():
    core = COMPOSE["services"]["core"]
    assert "agent-data:/data" in core["volumes"]
    assert "backups:/backups" in core["volumes"]
    assert "models:/models:ro" in core["volumes"]
    bind = next(value for value in core["volumes"] if isinstance(value, dict))
    assert bind["target"] == "/workspace" and bind["type"] == "bind"
    assert bind["bind"]["create_host_path"] is False
    assert core["environment"]["BACKUP_DIR"] == "/backups"
    assert set(COMPOSE["volumes"]) == {
        "agent-data", "backups", "browser-profile", "caddy-data", "caddy-config", "models",
    }
    assert COMPOSE["services"]["caddy"]["volumes"] == ["caddy-data:/data", "caddy-config:/config"]


def test_image_pins_and_nonroot_volume_ownership():
    caddy = (ROOT / "docker/caddy/Dockerfile").read_text()
    assert re.search(r"FROM caddy:2\.11\.7-alpine@sha256:[a-f0-9]{64}\n", caddy)
    assert "setcap -r /usr/bin/caddy" in caddy
    assert "chown -R 1000:1000 /data /config" in caddy and "USER 1000:1000" in caddy
    assert COMPOSE["services"]["caddy"]["sysctls"] == {"net.ipv4.ip_unprivileged_port_start": "0"}
    core = (ROOT / "Dockerfile").read_text()
    assert re.search(r"FROM python:3\.12-slim@sha256:[a-f0-9]{64}\n", core)
    assert "--require-hashes" in core and "--no-build-isolation --no-index" in core
    assert "chown -R agent:agent /data /backups /workspace" in core
    assert "chmod 0700 /data /backups /workspace" in core and "USER agent" in core
    assert "curl git jq" not in core
    # Core must not ship sandboxd; install strips it from pyproject packages.
    assert "COPY sandboxd" not in core
    assert 's/, "sandboxd"//' in core


def test_caddy_does_not_compress_chat_sse():
    caddyfile = (ROOT / "docker/caddy/Caddyfile").read_text()
    assert "flush_interval -1" in caddyfile
    assert "path_regexp chat_send" in caddyfile
    assert "encode @not_sse" in caddyfile


def test_model_is_pinned_isolated_and_readonly():
    model = COMPOSE["services"]["model"]
    version = dict(line.split("=", 1) for line in (ROOT / "docker/model/VERSION").read_text().splitlines())
    assert model["image"] == version["image"]
    assert re.fullmatch(r"ghcr.io/ggml-org/llama.cpp:server-b\d+@sha256:[a-f0-9]{64}", model["image"])
    assert model["networks"] == {"model": {"ipv4_address": "10.77.6.60"}}
    assert "ports" not in model and "env_file" not in model
    assert model["secrets"] == ["model_server_token"]
    assert model["volumes"][0] == "models:/models:ro"
    assert model["volumes"][1]["read_only"] is True
    assert model["oom_score_adj"] == 300
    assert model["pids_limit"] == 128
    assert model["healthcheck"]["start_period"] == "120s"
    script = (ROOT / "docker/model/run.sh").read_text()
    for setting in (
        "--flash-attn off",
        "--load-mode none",
        "--parallel 1",
        "--no-webui",
        "--no-agent",
        "--no-slots",
        "--cache-ram 0",
    ):
        assert setting in script


def test_resolved_default_hostname_and_resource_budget(resolved):
    services = resolved["services"]
    assert services["core"]["environment"]["AGENT_HOST"] == "37-60-226-214.sslip.io"
    assert services["caddy"]["environment"]["AGENT_HOST"] == "37-60-226-214.sslip.io"
    assert preflight.configuration_errors(resolved) == []
    # .env.example leaves the browser off: the service isn't part of the stack at all.
    assert set(services) == {"caddy", "core", "model", "sandbox"}
    assert services["core"]["environment"]["BROWSER_ENABLED"] == "false"
    assert sum(int(service["mem_limit"]) for service in services.values()) == 5600 * 1024 * 1024


@pytest.mark.parametrize(
    "domain,fallback,expected",
    [
        ("agent.example.test", "fallback.test", "agent.example.test"),
        ("", "fallback.test", "fallback.test"),
        ("", "", "37-60-226-214.sslip.io"),
    ],
)
def test_hostname_selection_applies_to_both_services(tmp_path, domain, fallback, expected):
    (tmp_path / ".env").write_text(f"AGENT_DOMAIN={domain}\nAGENT_FALLBACK_HOST={fallback}\n")
    env = {
        key: value for key, value in os.environ.items() if key not in {"AGENT_DOMAIN", "AGENT_FALLBACK_HOST"}
    }
    result = subprocess.run(
        compose_command()
        + [
            "-f",
            str(ROOT / "docker-compose.yml"),
            "--project-directory",
            str(tmp_path),
            "config",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        env=env,
    )
    config = json.loads(result.stdout)
    assert config["services"]["core"]["environment"]["AGENT_HOST"] == expected
    assert config["services"]["caddy"]["environment"]["AGENT_HOST"] == expected


@pytest.mark.parametrize(
    "change",
    [
        "port",
        "shell",
        "host",
        "tls",
        "privileged",
        "capability",
        "secret",
        "model_network",
        "model_write",
        "model_ctx",
        "sandbox_network",
    ],
)
def test_preflight_refuses_modified_policy_without_printing_secret_values(resolved, change):
    config = copy.deepcopy(resolved)
    core = config["services"]["core"]
    if change == "port":
        core["ports"] = [{"target": 8080, "published": "8080"}]
    elif change == "shell":
        core["environment"]["SHELL_BACKEND"] = "local"
    elif change == "host":
        core["environment"]["AGENT_HOST"] = "agent.test; script-src *"
    elif change == "tls":
        config["services"]["caddy"]["environment"]["CADDY_TLS"] = "disabled"
    elif change == "privileged":
        core["privileged"] = True
    elif change == "capability":
        core["cap_add"] = ["SYS_ADMIN"]
    elif change == "model_network":
        config["services"]["model"]["networks"]["public"] = {}
    elif change == "model_write":
        config["services"]["model"]["volumes"][0]["read_only"] = False
    elif change == "model_ctx":
        config["services"]["model"]["environment"]["MODEL_CTX"] = "8192"
    elif change == "sandbox_network":
        config["services"]["sandbox"]["networks"]["public"] = {}
    else:
        core["environment"]["MODEL_SERVER_TOKEN"] = "synthetic-value-must-not-be-printed"
    errors = preflight.configuration_errors(config)
    assert errors and "synthetic-value-must-not-be-printed" not in " ".join(errors)


def test_private_path_checks_metadata_and_refuses_symlinks(tmp_path):
    folder = tmp_path / "private"
    folder.mkdir(mode=0o700)
    file = folder / "token"
    file.write_text("synthetic-fixture")
    file.chmod(0o400)
    uid = os.getuid()
    assert preflight.private_path_errors(folder, directory=True, uid=uid) == []
    assert preflight.private_path_errors(file, directory=False, uid=uid) == []
    link = tmp_path / "link"
    link.symlink_to(file)
    assert preflight.private_path_errors(link, directory=False, uid=uid)
    file.chmod(0o644)
    assert preflight.private_path_errors(file, directory=False, uid=uid)
    assert preflight.private_path_errors(tmp_path / "missing", directory=False, uid=uid)


@pytest.mark.parametrize(
    "value,expected", [("v2.33.0", (2, 33, 0)), ("28.4.0", (28, 4, 0)), ("5.6.0-desktop.1", (5, 6, 0))]
)
def test_preflight_version_parser(value, expected):
    assert preflight.version(value) == expected


def test_preflight_cli_hides_compose_error_output(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(
            1, ["docker"], output="synthetic-private-value", stderr="synthetic-private-value"
        )

    monkeypatch.setattr(preflight.subprocess, "check_output", fail)
    assert preflight.main() == 1
    assert "synthetic-private-value" not in capsys.readouterr().err


def test_model_ctx_compose_default_is_4096():
    raw = (ROOT / "docker-compose.yml").read_text()
    assert "${MODEL_CTX:-4096}" in raw
    assert "${MODEL_CTX:-6144}" not in raw
    assert "${MODEL_CTX:-5120}" not in raw


def test_path_errors_requires_sandbox_api_token(tmp_path):
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    for name in ("agent_password_hash", "model_server_token"):
        path = secrets / name
        path.write_text("synthetic-fixture")
        path.chmod(0o400)
    config = {
        "secrets": {
            "agent_password_hash": {"file": str(secrets / "agent_password_hash")},
            "model_server_token": {"file": str(secrets / "model_server_token")},
        },
        "services": {"core": {"volumes": [{"source": str(workspace), "target": "/workspace"}]}},
    }
    # Force uid match for this process (CI/local may not be 1000).
    original = preflight.private_path_errors

    def check(path, *, directory, uid=1000):
        return original(path, directory=directory, uid=os.getuid())

    preflight.private_path_errors = check
    try:
        errors = preflight.path_errors(config)
        assert any("sandbox_api_token" in error for error in errors)
        # Core mounts the browser token even while the browser is off, so a deploy without
        # the file must stop at preflight, not when Compose recreates core.
        assert any("browser_api_token" in error for error in errors)
        for name in ("sandbox_api_token", "browser_api_token"):
            assert preflight.path_errors(config) != []
            token = secrets / name
            token.write_text("synthetic-fixture")
            token.chmod(0o400)
            config["secrets"][name] = {"file": str(token)}
        assert preflight.path_errors(config) == []
    finally:
        preflight.private_path_errors = original


def test_sandbox_service_hardening():
    sandbox = COMPOSE["services"]["sandbox"]
    assert sandbox["user"] == "1000:1000"
    assert sandbox["environment"]["SANDBOXD_HOST"] == "10.77.3.20"
    assert sandbox["environment"]["SANDBOXD_ALLOWED_PEERS"] == "10.77.3.10"
    assert sandbox["secrets"] == ["sandbox_api_token"]
    assert "env_file" not in sandbox
    assert "ports" not in sandbox
    assert sandbox["networks"]["sandbox_ctl"]["ipv4_address"] == "10.77.3.20"
    assert sandbox["networks"]["sandbox_egress"]["ipv4_address"] == "10.77.11.20"
    assert sandbox["oom_score_adj"] == 800
    assert sandbox["pids_limit"] == 256
    assert sandbox["mem_limit"] == "1g"
    dockerfile = (ROOT / "docker/sandbox/Dockerfile").read_text()
    assert "USER 1000:1000" in dockerfile
    assert "tini" in dockerfile
    assert "sandboxd" in dockerfile
    # Comment may name forbidden packages; the apt install block must not install them.
    run_lines: list[str] = []
    collecting = False
    for line in dockerfile.splitlines():
        if line.startswith("RUN apt-get"):
            collecting = True
        if collecting:
            run_lines.append(line)
            if not line.rstrip().endswith("\\"):
                break
    run_block = "\n".join(run_lines)
    assert "build-essential" not in run_block and "nodejs" not in run_block


# --- M6: the browser service ---

def test_browser_service_is_off_by_default_and_hardened():
    browser = COMPOSE["services"]["browser"]
    assert browser["profiles"] == ["browser"]  # not started unless .env sets COMPOSE_PROFILES=browser
    assert browser["user"] == "1000:1000" and browser["read_only"] is True and browser["cap_drop"] == ["ALL"]
    assert browser["security_opt"] == ["no-new-privileges:true", "seccomp=${BROWSER_SECCOMP:-builtin}"]
    assert browser["restart"] == "unless-stopped"
    assert not set(browser) & {"privileged", "network_mode", "pid", "ipc", "cap_add", "ports", "expose", "env_file"}
    assert "unconfined" not in json.dumps(browser) and "docker.sock" not in json.dumps(browser)
    assert browser["networks"] == {
        "browser_ctl": {"ipv4_address": "10.77.4.40"},
        "browser_egress": {"ipv4_address": "10.77.12.40", "gw_priority": 100},
    }  # the screen network joins in M7
    env = browser["environment"]
    assert env["BROWSERD_HOST"] == "10.77.4.40" and env["BROWSERD_PORT"] == "7100"
    assert env["BROWSERD_ALLOWED_PEERS"] == "10.77.4.10"
    assert env["BROWSER_API_TOKEN_FILE"] == "/run/secrets/browser_api_token"
    assert env["BROWSER_CHROMIUM_SANDBOX"] == "${BROWSER_CHROMIUM_SANDBOX:-false}"
    assert env["BROWSER_SENSITIVE_MATCH"] == "${BROWSER_SENSITIVE_MATCH:-substring}"  # the spec's rule
    assert env["BROWSER_BLOCK_BACKGROUND_POSTS"] == "${BROWSER_BLOCK_BACKGROUND_POSTS:-false}"
    assert env["BROWSER_MAX_DOWNLOAD_MB"] == "${BROWSER_MAX_DOWNLOAD_MB:-200}"
    assert env["DISPLAY"] == ":99" and env["HOME"] == "/tmp/home"
    assert "BROWSER_ALLOW_PRIVATE_HOSTS" not in env and not any(key.startswith("VNC") for key in env)
    assert browser["secrets"] == ["browser_api_token"]
    assert browser["healthcheck"]["test"] == ["CMD", "python", "-m", "browserd", "healthcheck"]
    assert (browser["mem_limit"], browser["memswap_limit"], browser["shm_size"]) == ("1280m", "1280m", "320m")
    assert (browser["cpus"], browser["pids_limit"], browser["oom_score_adj"]) == (2.0, 512, 500)
    assert browser["tmpfs"] == ["/tmp:size=256m,mode=1777"]
    assert browser["stop_grace_period"] == "30s"  # time for Chromium to write cookies to the profile


def test_browser_profile_is_mounted_into_the_browser_and_nowhere_else():
    """Security requirement 1 (docs/NEXT.md, M6): signed-in sessions never reach core, the
    sandbox or the workspace."""
    browser = COMPOSE["services"]["browser"]
    assert browser["volumes"][0] == "browser-profile:/profile"
    files = browser["volumes"][1]
    assert files["target"] == "/files" and files["bind"]["create_host_path"] is False
    assert files["source"] == "${WORKSPACE_HOST_DIR:-/srv/roland-agent/workspace}/browser"
    assert len(browser["volumes"]) == 2
    for name, service in COMPOSE["services"].items():
        if name != "browser":
            assert "browser-profile" not in json.dumps(service.get("volumes", [])), name
            assert "/profile" not in json.dumps(service.get("volumes", [])), name
    test_stack = yaml.safe_load((ROOT / "docker-compose.test.yml").read_text())
    for name, service in test_stack["services"].items():
        assert "browser-profile" not in json.dumps(service.get("volumes", [])), name


def test_private_addresses_are_only_let_through_in_the_test_stack():
    production = (ROOT / "docker-compose.yml").read_text()
    assert "BROWSER_ALLOW_PRIVATE_HOSTS" not in production
    test_stack = yaml.safe_load((ROOT / "docker-compose.test.yml").read_text())
    browser = test_stack["services"]["browser"]
    assert browser["environment"] == {"BROWSER_ALLOW_PRIVATE_HOSTS": "fixture-web", "BROWSER_MAX_DOWNLOAD_MB": "4"}
    assert browser["networks"] == {"fixtures": {"ipv4_address": "10.77.20.40"}}
    assert test_stack["networks"]["fixtures"]["internal"] is True
    tester = test_stack["services"]["tester"]
    assert tester["networks"]["browser_ctl"] == {"ipv4_address": "10.77.4.10"}  # stands in for core
    assert tester["read_only"] is True and tester["cap_drop"] == ["ALL"]


def test_browser_image_is_pinned_and_runs_as_a_normal_user():
    dockerfile = (ROOT / "docker/browser/Dockerfile").read_text()
    pinned = re.search(r"^FROM mcr\.microsoft\.com/playwright/python:v(\d+\.\d+\.\d+)-noble@sha256:[a-f0-9]{64}$",
                       dockerfile, re.M)
    assert pinned, "the browser image must be pinned by digest"
    lock = (ROOT / "requirements-browser.lock").read_text()
    # The pip package must be the version the image's Chromium was built for.
    assert f"playwright=={pinned.group(1)}" in lock
    assert f"playwright=={pinned.group(1)}" in (ROOT / "requirements-browser.in").read_text()
    assert "--require-hashes -r requirements-browser.lock" in dockerfile
    for line in lock.splitlines():
        if line and not line.startswith(("#", " ")):
            assert "==" in line and line.rstrip().endswith("\\"), line  # every package pinned and hashed
    assert "USER 1000:1000" in dockerfile and 'ENTRYPOINT ["tini", "--"]' in dockerfile
    assert 'CMD ["python", "-m", "browserd"]' in dockerfile
    assert "chown 1000:1000 /profile /files" in dockerfile and "chmod 0700 /profile" in dockerfile
    assert "/etc/opt/chrome_for_testing/policies/managed/" in dockerfile
    assert "COPY browserd ./browserd" in dockerfile and "COPY agent" not in dockerfile  # no core code in it
    for word in ("remote-debugging", "curl ", "sudo", "ssh", "--no-sandbox", "novnc"):
        assert word not in dockerfile, word
    tester = (ROOT / "docker/tester/Dockerfile").read_text()
    assert re.search(r"^FROM python:3\.12-slim@sha256:[a-f0-9]{64}$", tester, re.M)
    assert "--require-hashes" in tester and "USER 1000:1000" in tester


def test_chromium_policy_turns_off_what_could_keep_or_leak_secrets():
    policy = json.loads((ROOT / "docker/browser/chromium-policy.json").read_text())
    for name in ("PasswordManagerEnabled", "AutofillAddressEnabled", "AutofillCreditCardEnabled",
                 "MetricsReportingEnabled", "PromptForDownloadLocation", "PasswordLeakDetectionEnabled",
                 "SearchSuggestEnabled", "UrlKeyedAnonymizedDataCollectionEnabled", "BackgroundModeEnabled"):
        assert policy[name] is False, name
    assert policy["SyncDisabled"] is True and policy["BrowserSignin"] == 0
    assert policy["DefaultDownloadDirectory"] == "/files/downloads"
    assert policy["ExtensionInstallBlocklist"] == ["*"]
    assert set(policy["URLBlocklist"]) == {
        "file://*", "chrome://*", "devtools://*", "view-source:*", "chrome-extension://*",
    }
    # Setting this to 2 (as the spec asked) stops Playwright from starting the browser at all.
    assert "DeveloperToolsAvailability" not in policy


def test_seccomp_profile_for_chromiums_sandbox_is_the_reviewed_copy():
    """Playwright's published profile (v1.63.0 utils/docker/seccomp_profile.json, sha256
    cc3e61ca...7849) with one change: chroot no longer needs CAP_SYS_CHROOT, because the
    container drops every capability. Any other change must be deliberate: update the digest."""
    path = ROOT / "docker/browser/seccomp-chromium.json"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == (
        "1adbd78f69bfee4a4412cdcb595ded40e151e763e1749795db57501d4290c326"
    )
    profile = json.loads(path.read_text())
    assert profile["defaultAction"] == "SCMP_ACT_ERRNO"
    always = {name for rule in profile["syscalls"] if rule["action"] == "SCMP_ACT_ALLOW"
              and not rule.get("args") and not rule.get("includes") for name in rule["names"]}
    assert {"clone", "unshare", "setns", "chroot"} <= always  # what the sandbox needs
    for name in ("mount", "umount2", "bpf", "kexec_load", "init_module", "ptrace", "reboot", "swapon",
                 "open_by_handle_at", "perf_event_open", "pivot_root", "settimeofday"):
        assert name not in always, name  # still need a capability the container doesn't have


def test_resolved_stack_with_the_browser_switched_on(resolved_with_browser):
    services = resolved_with_browser["services"]
    assert set(services) == {"caddy", "core", "model", "sandbox", "browser"}
    assert services["core"]["environment"]["BROWSER_ENABLED"] == "true"
    assert preflight.configuration_errors(resolved_with_browser) == []
    assert sum(int(service["mem_limit"]) for service in services.values()) == (5600 + 1280) * 1024 * 1024
    browser = services["browser"]
    assert browser["security_opt"] == ["no-new-privileges:true", "seccomp=builtin"]
    assert browser["environment"]["BROWSER_CHROMIUM_SANDBOX"] == "false"
    workspace = next(m["source"] for m in services["core"]["volumes"] if m["target"] == "/workspace")
    assert {m["target"]: m["source"] for m in browser["volumes"]} == {
        "/profile": "browser-profile", "/files": f"{workspace}/browser",
    }
    assert not browser.get("ports")


def test_core_flag_without_the_service_is_refused(tmp_path):
    config = resolve(tmp_path, "\nBROWSER_ENABLED=true\n")
    assert "browser" not in config["services"]
    errors = preflight.configuration_errors(config)
    assert len(errors) == 1 and "COMPOSE_PROFILES=browser" in errors[0]


def test_chromium_sandbox_needs_its_seccomp_profile_and_only_that_one(tmp_path):
    base = "\nCOMPOSE_PROFILES=browser\nBROWSER_ENABLED=true\n"
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "c").mkdir()
    missing = resolve(tmp_path / "a", base + "BROWSER_CHROMIUM_SANDBOX=true\n")
    assert any("BROWSER_SECCOMP" in error for error in preflight.configuration_errors(missing))
    both = resolve(
        tmp_path / "b",
        base + "BROWSER_CHROMIUM_SANDBOX=true\nBROWSER_SECCOMP=./docker/browser/seccomp-chromium.json\n",
    )
    assert preflight.configuration_errors(both) == []
    other = resolve(tmp_path / "c", base + "BROWSER_SECCOMP=unconfined\n")
    errors = preflight.configuration_errors(other)
    assert any("security profile" in error or "seccomp" in error for error in errors)


@pytest.mark.parametrize(
    "change",
    ["port", "network", "peer", "private_hosts", "token", "profile_in_core", "profile_in_sandbox",
     "extra_mount", "files_elsewhere", "capability", "seccomp", "root", "writable", "limits", "url"],
)
def test_preflight_refuses_a_loosened_browser_service(resolved_with_browser, change):
    config = copy.deepcopy(resolved_with_browser)
    browser = config["services"]["browser"]
    if change == "port":
        browser["ports"] = [{"target": 7100, "published": "7100"}]
    elif change == "network":
        browser["networks"]["edge"] = {}
    elif change == "peer":
        browser["environment"]["BROWSERD_ALLOWED_PEERS"] = "10.77.4.10,10.77.12.1"
    elif change == "private_hosts":
        browser["environment"]["BROWSER_ALLOW_PRIVATE_HOSTS"] = "fixture-web"
    elif change == "token":
        browser["environment"]["BROWSER_API_TOKEN"] = "synthetic-value-must-not-be-printed"
    elif change == "profile_in_core":
        config["services"]["core"]["volumes"].append({"type": "volume", "source": "browser-profile", "target": "/p"})
    elif change == "profile_in_sandbox":
        config["services"]["sandbox"]["volumes"].append({"type": "bind", "source": "/x", "target": "/profile"})
    elif change == "extra_mount":
        browser["volumes"].append({"type": "bind", "source": "/srv", "target": "/srv"})
    elif change == "files_elsewhere":
        next(m for m in browser["volumes"] if m["target"] == "/files")["source"] = "/srv/roland-agent/workspace"
    elif change == "capability":
        browser["cap_add"] = ["SYS_ADMIN"]
    elif change == "seccomp":
        browser["security_opt"] = ["no-new-privileges:true", "seccomp=unconfined"]
    elif change == "root":
        browser["user"] = "0:0"
    elif change == "writable":
        browser["read_only"] = False
    elif change == "limits":
        del browser["mem_limit"]
    else:
        config["services"]["core"]["environment"]["BROWSER_URL"] = "http://browser.example:7100"
    errors = preflight.configuration_errors(config)
    assert errors and "synthetic-value-must-not-be-printed" not in " ".join(errors)
