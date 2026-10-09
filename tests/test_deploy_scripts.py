"""Syntax and policy checks for M2.5/M2.6 deploy scripts (no live host required)."""

from __future__ import annotations

import json
import os
import re
import signal
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"

SCRIPTS = [
    "common.sh",
    "secrets.sh",
    "hash-password.sh",
    "workspace-fs.sh",
    "firewall.sh",
    "preflight.sh",
    "deploy.sh",
    "ship.sh",
    "verify.sh",
    "restore.sh",
    "migrate-v1-workspace.sh",
    "model.sh",
    "memory-report.sh",
]


@pytest.mark.parametrize("name", SCRIPTS)
def test_deploy_scripts_exist_and_are_executable(name):
    path = DEPLOY / name
    assert path.is_file()
    mode = path.stat().st_mode
    if name == "common.sh":
        assert not (mode & stat.S_IXUSR), "common.sh is sourced, not executed"
    else:
        assert mode & stat.S_IXUSR


@pytest.mark.parametrize("name", SCRIPTS)
def test_bash_syntax(name):
    path = DEPLOY / name
    subprocess.run(["bash", "-n", str(path)], check=True, timeout=10)


def test_firewall_unit_exists():
    unit = DEPLOY / "roland-agent-firewall.service"
    text = unit.read_text()
    assert "After=docker.service ufw.service" in text
    assert "Type=oneshot" in text
    assert "RemainAfterExit=yes" in text
    assert "PartOf=docker.service" in text


def test_firewall_script_has_both_backends_and_safety():
    text = (DEPLOY / "firewall.sh").read_text()
    assert "--nft" in text
    assert "DOCKER-USER" in text
    assert "roland-agent" in text
    assert "APPLY" in text
    assert "iptables" in text and "nft" in text
    # Must not casually flush chains
    assert "iptables -F" not in text
    assert "iptables --flush" not in text


def test_secrets_script_never_prints_values_and_is_idempotent():
    text = (DEPLOY / "secrets.sh").read_text()
    assert "token_urlsafe" in text
    assert "vnc_password" in text
    assert "agent_password_hash" in text
    assert "Never print values" in text
    assert 'echo "$value"' not in text
    assert "printf '%s\n' \"$value\"" not in text


def test_workspace_fs_never_reformats_existing(tmp_path, monkeypatch):
    text = (DEPLOY / "workspace-fs.sh").read_text()
    assert "never reformat" in text.lower() or "Never reformats" in text
    assert "APPLY" in text
    # Dry-run without APPLY must not call sudo mkfs
    env = {**os.environ, "APPLY": "0", "WORKSPACE_BASE": str(tmp_path / "srv"), "WORKSPACE_HOST_DIR": str(tmp_path / "ws")}
    result = subprocess.run(
        ["bash", str(DEPLOY / "workspace-fs.sh"), "10"],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
        cwd=str(ROOT),
    )
    assert result.returncode != 0
    assert "APPLY=1" in result.stderr or "APPLY=1" in result.stdout


def test_workspace_fs_fstab_img_match_with_regex_metacharacters(tmp_path):
    """fstab img check must use field equality, not regex (GNU sed class bug)."""
    text = (DEPLOY / "workspace-fs.sh").read_text()
    assert "sed 's/[" not in text  # old broken character-class escape
    assert 'awk -v img="$img"' in text
    assert "$1 == img" in text

    # Path with regex metacharacters that would break a character-class escape.
    img = str(tmp_path / "workspace.(test)[0]+?.img")
    fstab = tmp_path / "fstab"
    # Uncommented line whose $1 equals img (different mount options than canonical).
    fstab.write_text(f"# comment with {img} decoy\n{img} /mnt/other ext4 loop 0 0\n")
    # Exact same awk used by workspace-fs.sh
    result = subprocess.run(
        ["awk", "-v", f"img={img}", "$1 == img { found=1; exit } END { exit !found }", str(fstab)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)

    # Commented-only mention must not match.
    fstab.write_text(f"# {img} /mnt/ws ext4 loop 0 0\n")
    result = subprocess.run(
        ["awk", "-v", f"img={img}", "$1 == img { found=1; exit } END { exit !found }", str(fstab)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode != 0

    # Missing img must not match.
    fstab.write_text("/other/path.img /mnt/ws ext4 loop 0 0\n")
    result = subprocess.run(
        ["awk", "-v", f"img={img}", "$1 == img { found=1; exit } END { exit !found }", str(fstab)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode != 0


def test_deploy_and_ship_refuse_without_apply():
    for name in ("deploy.sh", "ship.sh", "restore.sh", "migrate-v1-workspace.sh"):
        env = {**os.environ, "APPLY": "0"}
        if name == "ship.sh":
            env["HOST"] = "deploy@example.invalid"
        if name == "restore.sh":
            env["FILE"] = str(ROOT / "README.md")
        result = subprocess.run(
            ["bash", str(DEPLOY / name)],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
            cwd=str(ROOT),
        )
        assert result.returncode != 0, name
        assert "APPLY=1" in result.stderr, name


def test_firewall_dry_run_without_apply():
    result = subprocess.run(
        ["bash", str(DEPLOY / "firewall.sh"), "--iptables"],
        capture_output=True,
        text=True,
        env={**os.environ, "APPLY": "0"},
        timeout=15,
        cwd=str(ROOT),
    )
    assert result.returncode == 0
    assert "dry-run" in result.stdout.lower() or "would insert" in result.stdout


def test_secrets_creates_missing_files_without_printing(tmp_path):
    secrets = tmp_path / "secrets"
    env = {**os.environ, "SECRETS_DIR": str(secrets), "APPLY": "0", "FORCE": "0"}
    result = subprocess.run(
        ["bash", str(DEPLOY / "secrets.sh")],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stderr
    expected = {
        "model_server_token",
        "trainer_api_token",
        "sandbox_api_token",
        "browser_api_token",
        "vnc_password",
        "vnc_view_password",
    }
    names = {p.name for p in secrets.iterdir()}
    assert expected <= names
    for name in expected:
        path = secrets / name
        assert path.stat().st_size > 0
        assert stat.S_IMODE(path.stat().st_mode) == 0o400
        # Values must not appear in stdout/stderr
        value = path.read_text()
        assert value not in result.stdout
        assert value not in result.stderr
    # Idempotent second run
    again = subprocess.run(
        ["bash", str(DEPLOY / "secrets.sh")],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
        cwd=str(ROOT),
    )
    assert again.returncode == 0
    assert "keep existing" in again.stdout
    # VNC passwords differ
    assert (secrets / "vnc_password").read_text() != (secrets / "vnc_view_password").read_text()
    assert len((secrets / "vnc_password").read_text()) == 8


def test_makefile_does_not_export_empty_workspace_host_dir():
    """Empty exported WORKSPACE_HOST_DIR overrides Compose .env and breaks edge CI."""
    text = (ROOT / "Makefile").read_text()
    for line in text.splitlines():
        if line.startswith("export ") and "WORKSPACE_HOST_DIR" in line.split():
            raise AssertionError(f"Makefile must not export WORKSPACE_HOST_DIR: {line}")


def test_makefile_has_section_14_3_targets():
    text = (ROOT / "Makefile").read_text()
    for target in [
        "build",
        "up",
        "down",
        "ps",
        "logs",
        "deploy",
        "preflight",
        "secrets",
        "hash-password",
        "firewall",
        "firewall-install",
        "backup",
        "restore",
        "restore-test",
        "verify",
        "migrate-v1-workspace",
        "ship",
        "model-fetch",
        "model-install",
        "preflight-edge",
    ]:
        assert re.search(rf"^{re.escape(target)}:", text, re.M), target
    assert "model-bench" not in text  # deferred


def test_preflight_script_mentions_required_checks():
    text = (DEPLOY / "preflight.sh").read_text()
    for needle in [
        "Docker Engine",
        "Compose",
        "DOCKER-USER",
        "ufw",
        "0400",
        "15G",
        "swap",
        "workspace",
        "AGENT_HOST",
        "publishes host ports",
    ]:
        assert needle in text, needle


# These fake commands run only against a disposable repository. In particular,
# fake Docker records container commands without executing anything inside one.
FAKE_HOST_PROGRAM = r"""#!/usr/bin/env python3
import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

root = Path(os.environ["FAKE_HOST_DIR"])
state = json.loads((root / "state.json").read_text())
index = int((root / "sample").read_text())
sample = state["samples"][min(index, len(state["samples"]) - 1)]
command = Path(sys.argv[0]).name
args = sys.argv[1:]
with (root / "commands.jsonl").open("a") as stream:
    stream.write(json.dumps({"command": command, "args": args}) + "\n")

def answer(text="", code=0):
    if text:
        print(text)
    sys.exit(code)

def hang_until_killed():
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    (root / "hung-probe.pid").write_text(str(os.getpid()))
    while True:
        time.sleep(60)

services = state["services"]
ids = {name: f"{index:012x}" for index, name in enumerate(services, start=1)}
by_id = {value: name for name, value in ids.items()}
clock = root / "clock"
if command == "date" and args == ["+%s"]:
    answer(clock.read_text())
if command == "free":
    clock.write_text(str(int(clock.read_text()) + state.get("measurement_seconds", 0)))
    answer("              total used free shared buff/cache available\n"
           f"Mem:           8000 4000 1000 0 3000 {sample['available']}\nSwap: 2000 0 2000")
if command == "sleep":
    clock.write_text(str(int(clock.read_text()) + int(args[0])))
    (root / "sample").write_text(str(index + 1))
    answer()
if command == "sudo":
    answer(code=1)  # Never delegate to a real privileged command.
if command == "curl":
    if "-w" in args:  # verify.sh asks for the HTTP status of the screen routes
        answer(state.get("screen_http", {}).get(args[-1].rsplit("/screen/", 1)[-1], "403"))
    answer()  # Never contact the network.
if command == "df":
    answer("Filesystem 1024-blocks Used Available Capacity Mounted on\nfixture 70000000 1000000 69000000 2% /")
if command == "swapon":
    answer("/fixture-swap file 2G 0B -2")
if command in {"findmnt", "ss"}:
    answer()
if command == "stat":
    target = Path(args[-1])
    info = target.stat()
    fmt = args[args.index("-c") + 1]
    if fmt == "%a":
        answer(format(stat.S_IMODE(info.st_mode), "o"))
    if fmt == "%s":
        answer(str(info.st_size))
    if fmt == "%u":
        answer(str(state.get("browser_uid", 1000) if target.name == "browser_api_token"
                   else state.get("vnc_uid", 1000) if target.name.startswith("vnc_") else 1000))
if command == "docker":
    if args[0] == "volume":
        answer(args[-1], state.get("volume_rm_rc", 0) if args[1] == "rm" else 0)
    if args[0] == "run":
        answer(code=state.get("restore_rc", 0) if "restore" in args else 0)
    if args[0] == "info":
        answer("Firewall Backend: nftables")
    if args[0] == "version":
        answer("29.0.0")
    if args[0] == "stats":
        service = by_id[args[-1]]
        limits = {"caddy": 96, "core": 640, "model": 3840, "sandbox": 1024, "browser": 1280}
        limits.update(state.get("limits", {}))
        answer(state.get("stats_raw", f"{sample['usage'][service]}MiB / {limits[service]}MiB"), state.get("stats_rc", 0))
    if args[0] == "inspect":
        service = by_id[args[-1]]
        if "OOMKilled" in " ".join(args):
            full_id = f"{int(args[-1], 16):064x}"
            # The cgroup counter the script reads next, as the kernel would show it.
            events = root / "cgroup" / "system.slice" / f"docker-{full_id}.scope"
            events.mkdir(parents=True, exist_ok=True)
            kills = sample.get("oom_kills", {}).get(service, 0)
            (events / "memory.events").write_text(f"low 0\nhigh 0\nmax 3\noom 1\noom_kill {kills}\n")
            oom = "true" if sample.get("oom", {}).get(service, False) else "false"
            restarts = sample.get("restarts", {}).get(service, 0)
            started = sample.get("started", {}).get(service, "2026-10-08T05:00:00.123456789Z")
            answer(f"{full_id} {oom} {restarts} {started}")
        if "LogConfig" in " ".join(args):
            answer(json.dumps(state.get("log_config", {"Type": "json-file", "Config": {"max-size": "10m", "max-file": "3"}})))
    if args[0] == "ps":
        answer()  # No published ports in this fixture.
    if args[0] == "compose":
        if "version" in args:
            answer("2.35.0")
        if "config" in args:
            answer("AGENT_HOST: nested-probe.example")
        if "ps" in args:
            if state.get("hang_compose_ps"):
                hang_until_killed()
            if state.get("compose_ps_rc"):
                answer(code=state["compose_ps_rc"])
            if "--all" in args and "json" not in args:
                answer("\n".join(f"{name}|{ids[name]}|{status}" for name, status in services.items()))
            if "--services" in args:
                answer("\n".join(name for name, status in services.items() if status == "running"))
            if "-q" in args:
                answer("\n".join(ids[name] for name, status in services.items() if status == "running"))
            if "--format" in args:
                answer("\n".join(json.dumps({"Service": name, "State": status, "Health": "healthy"}) for name, status in services.items()))
            answer()
        if "exec" in args:
            tail = args[args.index("exec") + 1:]
            interactive = True
            while tail[0] in {"-T", "--interactive=false"}:
                if tail[0] == "--interactive=false":
                    interactive = False
                tail = tail[1:]
            service, action = tail[0], tail[1:]
            if state.get("compose_reads_stdin") and interactive:
                (root / "hung-probe.pid").write_text(str(os.getpid()))
                # Compose attaches stdin by default even with -T. Reading all
                # input blocks when the caller keeps its terminal/pipe open.
                sys.stdin.buffer.read()
                (root / "stdin-read").touch()
            if state.get("hang_docker_service") == service:
                hang_until_killed()
            if service == "browser" and action == ["printenv", "SCREEN_ENABLED"]:
                answer(state.get("browser_screen_env", "true"))
            if service == "browser" and action[-2:] == ["healthcheck", "screen"]:
                answer(code=state.get("screen_health_rc", 0))
            if service == "browser" and "healthcheck" in action:
                answer(code=state.get("browser_health_rc", 0))
            if service == "browser" and "-c" in action:
                host, port = action[-2:]
                answer(code=state.get("browser_egress_rc", 0) if host == "example.com" else state.get("browser_probe_rc", 42))
            if service == "sandbox" and "curl" in action:
                if state.get("hang_sandbox_command") and "http://core:8080/healthz" in action:
                    # Simulate an exec'd command with the real in-container timeout,
                    # replacing only curl with a disposable child that ignores TERM.
                    child = [sys.executable, "-c", '''
import os, signal, time
from pathlib import Path
root = Path(os.environ["FAKE_HOST_DIR"])
signal.signal(signal.SIGTERM, lambda *_: (root / "probe-term").touch())
(root / "hung-probe.pid").write_text(str(os.getpid()))
while True:
    time.sleep(60)
''']
                    prefix = action[:action.index("curl")]
                    wait_status = subprocess.call(prefix + child)
                    # Docker exec reports 128 + signal; Python wait uses -signal.
                    status = 128 - wait_status if wait_status < 0 else wait_status
                    (root / "probe-exited").write_text(str(status))
                    answer(code=status)
                answer(code=state.get("sandbox_egress_rc", 0) if any("example.com" in arg for arg in action)
                       else state.get("sandbox_probe_rc", 7))
            if service == "model":
                answer(code=state.get("model_health_rc", 0) if "healthcheck" in action else state.get("model_egress_rc", 42))
            if service == "core" and "/backups/db" in " ".join(action):
                answer(code=state.get("backup_rc", 0))
answer(f"Unexpected fake command: {command} {args}", code=90)
"""


def fake_deploy_host(tmp_path, *, browser=False, samples=None):
    """Copy read-only scripts; route their host/container observations to fakes."""
    repo = tmp_path / "repo"
    deploy = repo / "deploy"
    deploy.mkdir(parents=True)
    for name in ("common.sh", "memory-report.sh", "preflight.sh", "verify.sh", "verify_state.py", "restore.sh"):
        (deploy / name).write_bytes((DEPLOY / name).read_bytes())
    (repo / "docker-compose.yml").write_text("services: {}\n")
    isolation = repo / "tests/integration/isolation.sh"
    isolation.parent.mkdir(parents=True)
    isolation.write_text("#!/usr/bin/env bash\nexit 0\n")
    isolation.chmod(0o755)
    (deploy / "preflight_edge.py").write_text("# Edge policy is outside these script branch tests.\n")
    services = dict.fromkeys(("caddy", "core", "model", "sandbox"), "running")
    usage = {"caddy": 64, "core": 300, "model": 3500, "sandbox": 128}
    if browser:
        services["browser"] = "running"
        usage["browser"] = 700
    state = {"services": services, "samples": samples or [{"available": 2400, "usage": usage}]}
    (tmp_path / "state.json").write_text(json.dumps(state))
    (tmp_path / "sample").write_text("0")
    (tmp_path / "clock").write_text("0")
    (tmp_path / "commands.jsonl").write_text("")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for command in (
        "docker",
        "free",
        "sleep",
        "date",
        "sudo",
        "curl",
        "df",
        "swapon",
        "findmnt",
        "ss",
        "stat",
    ):
        executable = bin_dir / command
        executable.write_text(FAKE_HOST_PROGRAM, newline="\n")
        executable.chmod(0o755)
    workspace = repo / "workspace"
    workspace.mkdir(mode=0o700)
    secrets = repo / "secrets"
    secrets.mkdir(mode=0o700)
    for name in ("model_server_token", "agent_password_hash"):
        secret = secrets / name
        secret.write_text("synthetic-fixture-only")
        secret.chmod(0o400)
    env_file = repo / ".env"
    env_file.write_text(
        "AGENT_FALLBACK_HOST=fixture.invalid\nAGENT_HOST=fixture.invalid\nCADDY_TLS=internal\n"
        "MODEL_PROVIDER=llamacpp\nMODEL_BASE_URL=http://model:8080/v1\n"
    )
    env_file.chmod(0o600)
    env = {
        **os.environ,
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "FAKE_HOST_DIR": str(tmp_path),
        "WORKSPACE_HOST_DIR": str(workspace),
        "APPLY": "0",
        "WATCH": "0",
        "MEMORY_REPORT_CGROUP_ROOT": str(tmp_path / "cgroup"),
        "BROWSER_CAP_MIB": "1280",
        "MIN_AVAILABLE_MIB": "800",
        "AGENT_ENV": "",
        "STRICT_SECRETS": "0",
    }
    return repo, env, state


def run_fake_script(repo, env, name, *args):
    return subprocess.run(
        ["bash", str(repo / "deploy" / name), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=repo,
        timeout=20,
    )


def fake_commands(env):
    return [
        json.loads(line) for line in (Path(env["FAKE_HOST_DIR"]) / "commands.jsonl").read_text().splitlines()
    ]


def update_fake_state(env, state):
    (Path(env["FAKE_HOST_DIR"]) / "state.json").write_text(json.dumps(state))


def memory_sample(available, browser_mb=None, *, oom=None, core_mb=300):
    usage = {"caddy": 64, "core": core_mb, "model": 3500, "sandbox": 128}
    if browser_mb is not None:
        usage["browser"] = browser_mb
    return {"available": available, "usage": usage, "oom": oom or {}}


@pytest.mark.parametrize(("available", "passed"), [(2080, True), (2079, False)])
def test_memory_report_browser_off_projects_cap_before_enabling(tmp_path, available, passed):
    repo, env, _ = fake_deploy_host(tmp_path, samples=[memory_sample(available)])
    before = (repo / ".env").read_bytes()
    result = run_fake_script(repo, env, "memory-report.sh")
    assert (result.returncode == 0) is passed, result.stdout + result.stderr
    assert f"projected minimum available={available - 1280} MiB after 1280 MiB browser cap" in result.stdout
    assert ("PRE-STEP PASS" in result.stdout) is passed
    assert ("PRE-STEP FAIL" in result.stdout) is not passed
    assert (repo / ".env").read_bytes() == before
    commands = fake_commands(env)
    assert not any(command["command"] == "sleep" for command in commands)
    # The measurement must neither start containers nor inspect their env/secrets.
    docker_calls = [command["args"] for command in commands if command["command"] == "docker"]
    assert docker_calls
    for args in docker_calls:
        if args[0] == "compose":
            assert "ps" in args and not any(action in args for action in ("up", "down", "exec", "restart"))
        elif args[0] == "inspect":
            assert args[1:3] == ["--format", "{{.Id}} {{.State.OOMKilled}} {{.RestartCount}} {{.State.StartedAt}}"]
        else:
            assert args[0] == "stats"


@pytest.mark.parametrize(("available", "passed"), [(1412, True), (1411, False)])
def test_memory_report_respects_custom_mib_cap_and_minimum(tmp_path, available, passed):
    repo, env, _ = fake_deploy_host(tmp_path, samples=[memory_sample(available)])
    env.update(BROWSER_CAP_MIB="512", MIN_AVAILABLE_MIB="900")
    result = run_fake_script(repo, env, "memory-report.sh")
    assert (result.returncode == 0) is passed, result.stdout + result.stderr
    assert f"projected minimum available={available - 512} MiB after 512 MiB browser cap" in result.stdout
    assert ("PRE-STEP PASS" in result.stdout) is passed


@pytest.mark.parametrize(
    ("available", "peak", "passed", "message"),
    [
        (800, 1280, True, "A6.5 PASS"),
        (800, 1280.01, False, "browser peak exceeds cap"),
        (799, 700, False, "host available memory fell below 800 MiB"),
    ],
)
def test_memory_report_running_browser_enforces_cap_and_headroom(tmp_path, available, peak, passed, message):
    repo, env, _ = fake_deploy_host(tmp_path, browser=True, samples=[memory_sample(available, peak)])
    result = run_fake_script(repo, env, "memory-report.sh")
    assert (result.returncode == 0) is passed, result.stdout + result.stderr
    assert message in result.stdout
    assert ("A6.5 PASS" in result.stdout) is passed
    assert ("A6.5 FAIL" in result.stdout) is not passed
    assert "browser off: projected" not in result.stdout
    assert f"minimum host available={available} MiB" in result.stdout


@pytest.mark.parametrize(
    ("limit", "passed"),
    [(1280, True), (1024, True), (1281, False), (2048, False), (7938, False)],
)
def test_memory_report_rejects_a_browser_limit_above_the_cap(tmp_path, limit, passed):
    """A low usage sample must not certify a browser that is unlimited (Docker then reports
    the host's memory as the limit) or has a larger limit than the cap being checked."""
    repo, env, state = fake_deploy_host(tmp_path, browser=True, samples=[memory_sample(2400, 300)])
    state["limits"] = {"browser": limit}
    update_fake_state(env, state)
    result = run_fake_script(repo, env, "memory-report.sh")
    assert (result.returncode == 0) is passed, result.stdout + result.stderr
    assert ("A6.5 PASS" in result.stdout) is passed
    assert (f"browser memory limit {limit}.00 MiB is above the 1280 MiB cap" in result.stdout) is not passed


def test_memory_report_cap_setting_also_applies_to_the_limit_check(tmp_path):
    repo, env, _ = fake_deploy_host(tmp_path, browser=True, samples=[memory_sample(2400, 300)])
    env["BROWSER_CAP_MIB"] = "1024"
    result = run_fake_script(repo, env, "memory-report.sh")
    assert result.returncode != 0
    assert "browser memory limit 1280.00 MiB is above the 1024 MiB cap" in result.stdout
    assert "A6.5 FAIL" in result.stdout


@pytest.mark.parametrize("service", ["browser", "model"])
def test_memory_report_fails_for_observed_oom_kills(tmp_path, service):
    repo, env, _ = fake_deploy_host(
        tmp_path,
        browser=True,
        samples=[memory_sample(1800, 700, oom={service: True})],
    )
    result = run_fake_script(repo, env, "memory-report.sh")
    assert result.returncode != 0
    assert re.search(rf"service={service} state=running .*OOMKilled=true", result.stdout)
    assert "OOMKilled observed during browser runtime report" in result.stdout
    assert "A6.5 FAIL" in result.stdout


def restarted_sample(available, service, *, restarts=1, browser_mb=None):
    sample = memory_sample(available, browser_mb)
    sample["restarts"] = {service: restarts}
    sample["started"] = {service: "2026-10-08T05:31:40.000000001Z"}
    return sample


@pytest.mark.parametrize("browser", [False, True])
def test_memory_report_fails_when_a_service_restarts_during_the_observation(tmp_path, browser):
    """Seen on Contabo: the model died under load and Docker restarted it between two samples.
    OOMKilled was false on every sample (Docker clears it on restart), and the report passed."""
    browser_mb = 700 if browser else None
    samples = [
        memory_sample(3400, browser_mb),
        restarted_sample(7100, "model", browser_mb=browser_mb),
        restarted_sample(3300, "model", browser_mb=browser_mb),
    ]
    repo, env, _ = fake_deploy_host(tmp_path, browser=browser, samples=samples)
    env["WATCH"] = "10"
    result = run_fake_script(repo, env, "memory-report.sh")
    verdict = "A6.5" if browser else "PRE-STEP"
    assert result.returncode != 0, result.stdout
    assert f"{verdict} FAIL" in result.stdout
    assert result.stdout.count("model restarted during the observation") == 1
    assert re.search(r"service=model state=running .*OOMKilled=false restarts=1 ", result.stdout)


def test_memory_report_notes_but_accepts_restarts_from_before_it_started(tmp_path):
    sample = restarted_sample(3400, "model", restarts=2)
    repo, env, _ = fake_deploy_host(tmp_path, samples=[sample, sample])
    env["WATCH"] = "5"
    result = run_fake_script(repo, env, "memory-report.sh")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[INFO] model restarted 2 time(s) before this report" in result.stdout
    assert "PRE-STEP PASS" in result.stdout


@pytest.mark.parametrize("service", ["browser", "sandbox"])
def test_memory_report_fails_for_a_process_killed_inside_a_container(tmp_path, service):
    """A Chromium renderer or a sandbox job killed for lack of memory leaves the container
    running and OOMKilled false; only the cgroup's oom_kill counter shows it."""
    later = memory_sample(1800, 700)
    later["oom_kills"] = {service: 1}
    samples = [memory_sample(1800, 700), later, later]
    repo, env, _ = fake_deploy_host(tmp_path, browser=True, samples=samples)
    env["WATCH"] = "10"
    result = run_fake_script(repo, env, "memory-report.sh")
    assert result.returncode != 0, result.stdout
    assert result.stdout.count(f"{service}: a process was killed for lack of memory during the observation") == 1
    assert re.search(rf"service={service} state=running .*oom_kills=1", result.stdout)
    assert "A6.5 FAIL" in result.stdout


def test_memory_report_counts_kills_from_before_it_started_against_the_browser(tmp_path):
    sample = memory_sample(1800, 700)
    sample["oom_kills"] = {"browser": 2}
    repo, env, _ = fake_deploy_host(tmp_path, browser=True, samples=[sample])
    result = run_fake_script(repo, env, "memory-report.sh")
    assert result.returncode != 0, result.stdout
    assert "[INFO] browser: 2 process(es) killed for lack of memory since it started" in result.stdout
    assert "A6.5 FAIL" in result.stdout


def test_memory_report_without_a_readable_kill_counter_still_reports(tmp_path):
    repo, env, _ = fake_deploy_host(tmp_path, samples=[memory_sample(3400)])
    env["MEMORY_REPORT_CGROUP_ROOT"] = str(tmp_path / "no-such-cgroup")
    result = run_fake_script(repo, env, "memory-report.sh")
    assert result.returncode == 0, result.stdout + result.stderr
    assert re.search(r"service=model state=running .*oom_kills=unavailable", result.stdout)
    assert "PRE-STEP PASS" in result.stdout


def test_memory_report_watch_keeps_transient_peaks_and_minimum_headroom(tmp_path):
    samples = [memory_sample(1600, 600), memory_sample(900, 1100, core_mb=420), memory_sample(1200, 700)]
    repo, env, _ = fake_deploy_host(tmp_path, browser=True, samples=samples)
    env["WATCH"] = "10"
    result = run_fake_script(repo, env, "memory-report.sh")
    assert result.returncode == 0, result.stdout + result.stderr
    assert re.findall(r"Sample t=(\d+)s", result.stdout) == ["0", "5", "10"]
    assert "service=browser peak=1100.00 MiB" in result.stdout
    assert "service=core peak=420.00 MiB" in result.stdout
    assert "minimum host available=900 MiB" in result.stdout
    commands = fake_commands(env)
    assert [command["args"] for command in commands if command["command"] == "sleep"] == [["5"], ["5"]]
    assert len([command for command in commands if command["command"] == "free"]) == 3


def test_memory_report_watch_accounts_for_time_spent_measuring(tmp_path):
    samples = [memory_sample(1600, 600), memory_sample(900, 1100), memory_sample(1200, 700)]
    repo, env, state = fake_deploy_host(tmp_path, browser=True, samples=samples)
    state["measurement_seconds"] = 2
    update_fake_state(env, state)
    env["WATCH"] = "10"
    result = run_fake_script(repo, env, "memory-report.sh")
    assert result.returncode == 0, result.stdout + result.stderr
    assert re.findall(r"Sample t=(\d+)s", result.stdout) == ["0", "5", "10"]
    sleeps = [command["args"] for command in fake_commands(env) if command["command"] == "sleep"]
    assert sleeps == [["3"], ["3"]]
    # The final measurement costs two seconds; earlier measurement time consumes
    # the watch budget instead of being added to ten seconds of sleeps.
    assert (Path(env["FAKE_HOST_DIR"]) / "clock").read_text() == "12"
    assert "service=browser peak=1100.00 MiB" in result.stdout


@pytest.mark.parametrize(
    ("peak", "oom", "message"),
    [
        (1300, False, "browser peak exceeds cap"),
        (700, True, "OOMKilled observed during browser runtime report"),
    ],
)
def test_memory_report_watch_does_not_forget_a_recovered_failure(tmp_path, peak, oom, message):
    samples = [
        memory_sample(1800, 600),
        memory_sample(1400, peak, oom={"browser": oom}),
        memory_sample(1600, 650),
    ]
    repo, env, _ = fake_deploy_host(tmp_path, browser=True, samples=samples)
    env["WATCH"] = "7"
    result = run_fake_script(repo, env, "memory-report.sh")
    assert result.returncode != 0
    assert message in result.stdout
    assert re.findall(r"Sample t=(\d+)s", result.stdout) == ["0", "5", "7"]
    assert [command["args"] for command in fake_commands(env) if command["command"] == "sleep"] == [
        ["5"],
        ["2"],
    ]


@pytest.mark.parametrize("failure", ["stats", "malformed", "containers"])
def test_memory_report_missing_measurements_cannot_pass(tmp_path, failure):
    repo, env, state = fake_deploy_host(tmp_path)
    if failure == "stats":
        state["stats_rc"] = 1
    elif failure == "malformed":
        state["stats_raw"] = "unavailable"
    else:
        state["services"] = {}
    update_fake_state(env, state)
    result = run_fake_script(repo, env, "memory-report.sh")
    assert result.returncode != 0
    assert "PRE-STEP FAIL" in result.stdout
    messages = {
        "stats": "docker stats unavailable",
        "malformed": "invalid memory usage",
        "containers": "no Compose containers found",
    }
    assert messages[failure] in result.stdout


@pytest.mark.parametrize("model_state", [None, "exited"])
def test_memory_report_requires_the_baseline_model_to_be_running(tmp_path, model_state):
    repo, env, state = fake_deploy_host(tmp_path)
    if model_state is None:
        del state["services"]["model"]
    else:
        state["services"]["model"] = model_state
    update_fake_state(env, state)
    result = run_fake_script(repo, env, "memory-report.sh")
    assert result.returncode != 0, result.stdout + result.stderr
    assert "PRE-STEP FAIL" in result.stdout
    assert "baseline service model is missing or not running" in result.stdout
    assert "PRE-STEP PASS" not in result.stdout


def copy_fake_isolation(repo):
    destination = repo / "tests" / "integration" / "isolation.sh"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes((ROOT / "tests" / "integration" / "isolation.sh").read_bytes())
    destination.chmod(0o755)
    return destination


def run_fake_isolation(repo, env, *args):
    destination = copy_fake_isolation(repo)
    return subprocess.run(
        ["bash", str(destination), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=repo,
        timeout=20,
    )


def run_fake_with_open_stdin(repo, env, script, *args):
    command = ["bash", str(script), *args]
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env, cwd=repo,
    )
    try:
        # Keep stdin open while waiting: communicate() would close it and hide
        # an unintended Docker attachment to the caller's input.
        process.wait(timeout=15)
        stdout, stderr = process.communicate(timeout=5)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        cleanup_hung_fake_probe(env)
        if process.poll() is None:
            if process.stdin:
                process.stdin.close()
            process.kill()
            process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream:
                stream.close()


@pytest.mark.parametrize("mode", ["--ci", "--server"])
def test_isolation_finishes_without_attaching_to_the_callers_open_input(tmp_path, mode):
    repo, env, state = fake_deploy_host(tmp_path, browser=True)
    state["compose_reads_stdin"] = True
    update_fake_state(env, state)
    result = run_fake_with_open_stdin(repo, env, copy_fake_isolation(repo), mode)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "all checks passed" in result.stdout
    assert not (Path(env["FAKE_HOST_DIR"]) / "stdin-read").exists()


def test_verify_finishes_with_the_callers_input_still_open(tmp_path):
    repo, env, state = fake_deploy_host(tmp_path, browser=True)
    state["compose_reads_stdin"] = True
    update_fake_state(env, state)
    copy_fake_isolation(repo)
    result = run_fake_with_open_stdin(repo, env, repo / "deploy" / "verify.sh")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[PASS] isolation.sh --server" in result.stdout
    assert "[PASS] model healthcheck" in result.stdout
    assert re.search(r"verify: \d+ pass / 0 fail / \d+ skip", result.stdout)


def browser_execs(env):
    return [
        command["args"]
        for command in fake_commands(env)
        if command["command"] == "docker" and "exec" in command["args"] and "browser" in command["args"]
    ]


def test_isolation_skips_an_absent_browser_without_probing_it(tmp_path):
    repo, env, _ = fake_deploy_host(tmp_path)
    result = run_fake_isolation(repo, env, "--ci")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SKIP browser isolation: browser service is not running\n" in result.stdout
    assert browser_execs(env) == []
    assert not any("169.254.169.254" in " ".join(command["args"]) for command in fake_commands(env))


@pytest.mark.parametrize("mode", ["--ci", "--server"])
def test_browser_isolation_probes_expected_targets_in_each_mode(tmp_path, mode):
    repo, env, _ = fake_deploy_host(tmp_path, browser=True)
    result = run_fake_isolation(repo, env, mode)
    assert result.returncode == 0, result.stdout + result.stderr
    targets = {tuple(args[-2:]) for args in browser_execs(env)}
    expected = {
        ("10.77.4.10", "8080"),
        ("10.77.1.10", "8080"),
        ("10.77.3.20", "7000"),
        ("10.77.6.60", "8080"),
    }
    if mode == "--server":
        expected |= {
            ("169.254.169.254", "80"),
            ("10.77.12.1", "22"),
            ("37.60.226.214", "22"),
            ("example.com", "443"),
        }
    assert targets == expected
    assert "SKIP browser isolation" not in result.stdout
    metadata_calls = [
        command for command in fake_commands(env) if "169.254.169.254" in " ".join(command["args"])
    ]
    assert bool(metadata_calls) is (mode == "--server")


@pytest.mark.parametrize("stopped", ["core", "model"])
def test_server_isolation_fails_when_a_probed_service_is_down(tmp_path, stopped):
    """A blocked connection to a service that isn't running proves nothing. On the server
    that must fail the run, not pass it."""
    repo, env, state = fake_deploy_host(tmp_path, browser=True)
    state["services"][stopped] = "exited"
    update_fake_state(env, state)
    result = run_fake_isolation(repo, env, "--server")
    assert result.returncode != 0
    assert f"FAIL isolation: {stopped} is not running" in result.stderr
    assert "all checks passed" not in result.stdout


def test_partial_stack_skips_probes_of_services_that_are_down(tmp_path):
    """In a test stack without core or model, their probes are skipped and said to be."""
    repo, env, state = fake_deploy_host(tmp_path, browser=True)
    state["services"]["core"] = "exited"
    del state["services"]["model"]
    update_fake_state(env, state)
    result = run_fake_isolation(repo, env, "--ci")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SKIP probes of core: it is not running\n" in result.stdout
    assert "SKIP probes of model: it is not running\n" in result.stdout
    assert {tuple(args[-2:]) for args in browser_execs(env)} == {("10.77.3.20", "7000")}
    sandbox_probes = " ".join(
        " ".join(command["args"]) for command in fake_commands(env)
        if command["command"] == "docker" and "exec" in command["args"] and "sandbox" in command["args"]
    )
    assert "10.77.4.40:7100" in sandbox_probes and "core:8080" not in sandbox_probes
    assert "10.77.1.10" not in sandbox_probes and "10.77.3.10" not in sandbox_probes


def test_sandbox_to_browserd_probe_is_skipped_without_the_browser(tmp_path):
    repo, env, _ = fake_deploy_host(tmp_path)
    result = run_fake_isolation(repo, env, "--server")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SKIP sandbox to browserd: browser service is not running\n" in result.stdout
    assert not any("10.77.4.40" in " ".join(command["args"]) for command in fake_commands(env))


def test_isolation_can_probe_a_named_test_stack(tmp_path):
    repo, env, _ = fake_deploy_host(tmp_path, browser=True)
    env["ISOLATION_COMPOSE_FILES"] = "docker-compose.yml docker-compose.test.yml"
    result = run_fake_isolation(repo, env, "--ci")
    assert result.returncode == 0, result.stdout + result.stderr
    compose_calls = [
        command["args"] for command in fake_commands(env)
        if command["command"] == "docker" and command["args"][0] == "compose"
    ]
    assert compose_calls
    for args in compose_calls:
        assert args[1:5] == ["-f", "docker-compose.yml", "-f", "docker-compose.test.yml"]


@pytest.mark.parametrize("code", [0, 1, 124])
def test_browser_isolation_rejects_connect_success_and_probe_errors(tmp_path, code):
    repo, env, state = fake_deploy_host(tmp_path, browser=True)
    state["browser_probe_rc"] = code
    update_fake_state(env, state)
    result = run_fake_isolation(repo, env, "--ci")
    assert result.returncode != 0
    assert f"probe exit {code}; expected blocked connection" in result.stderr
    assert "all checks passed" not in result.stdout


@pytest.mark.parametrize("code", [6, 7, 28])
def test_sandbox_isolation_accepts_only_network_block_errors(tmp_path, code):
    repo, env, state = fake_deploy_host(tmp_path)
    state["sandbox_probe_rc"] = code
    update_fake_state(env, state)
    result = run_fake_isolation(repo, env, "--server")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok fail: curl core by name\n" in result.stdout
    assert "all checks passed" in result.stdout


@pytest.mark.parametrize("code", [0, 1, 22, 52, 56, 124, 125, 126, 127, 137])
def test_sandbox_isolation_rejects_success_http_errors_and_broken_probes(tmp_path, code):
    repo, env, state = fake_deploy_host(tmp_path)
    state["sandbox_probe_rc"] = code
    update_fake_state(env, state)
    result = run_fake_isolation(repo, env, "--server")
    assert result.returncode != 0
    assert f"probe exit {code}; expected blocked connection" in result.stderr
    assert "ok fail: curl core by name" not in result.stdout
    assert "all checks passed" not in result.stdout


@pytest.mark.parametrize("service", ["sandbox", "browser"])
@pytest.mark.parametrize("code", [7, 124, 137])
def test_isolation_rejects_failed_or_timed_out_egress(tmp_path, service, code):
    repo, env, state = fake_deploy_host(tmp_path, browser=True)
    state[f"{service}_egress_rc"] = code
    update_fake_state(env, state)
    result = run_fake_isolation(repo, env, "--server")
    assert result.returncode != 0
    label = "curl example.com" if service == "sandbox" else "browser to example.com:443"
    assert f"FAIL isolation: {label} unexpectedly failed" in result.stderr
    assert "all checks passed" not in result.stdout


def cleanup_hung_fake_probe(env):
    """Clean up only the child this fixture created if an old script left it behind."""
    pid_file = Path(env["FAKE_HOST_DIR"]) / "hung-probe.pid"
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text()), signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_isolation_stops_a_term_ignoring_command_inside_sandbox(tmp_path):
    repo, env, state = fake_deploy_host(tmp_path)
    state["hang_sandbox_command"] = True
    update_fake_state(env, state)
    try:
        result = run_fake_isolation(repo, env, "--ci")
        assert result.returncode != 0
        assert "probe exit 137; expected blocked connection" in result.stderr
        assert "ok fail: curl core by name" not in result.stdout
        assert "all checks passed" not in result.stdout
        fixture = Path(env["FAKE_HOST_DIR"])
        assert (fixture / "probe-term").exists(), "the simulated container command received TERM"
        assert (fixture / "probe-exited").read_text() == "137", "inner timeout killed it before Docker's deadline"
    finally:
        cleanup_hung_fake_probe(env)


@pytest.mark.parametrize("service", ["sandbox", "browser"])
def test_isolation_kills_a_stuck_docker_exec_and_fails(tmp_path, service):
    repo, env, state = fake_deploy_host(tmp_path, browser=True)
    state["hang_docker_service"] = service
    update_fake_state(env, state)
    try:
        result = run_fake_isolation(repo, env, "--ci")
        assert result.returncode != 0
        assert "probe exit 137; expected blocked connection" in result.stderr
        assert "all checks passed" not in result.stdout
        assert (Path(env["FAKE_HOST_DIR"]) / "hung-probe.pid").exists()
    finally:
        cleanup_hung_fake_probe(env)


@pytest.mark.parametrize("failure", ["error", "hang"])
def test_isolation_fails_if_listing_compose_services_breaks_or_hangs(tmp_path, failure):
    repo, env, state = fake_deploy_host(tmp_path)
    state["compose_ps_rc"] = 1 if failure == "error" else 0
    state["hang_compose_ps"] = failure == "hang"
    update_fake_state(env, state)
    try:
        result = run_fake_isolation(repo, env, "--ci")
        assert result.returncode != 0
        assert "FAIL isolation: cannot list running Compose services" in result.stderr
        assert "all checks passed" not in result.stdout
    finally:
        cleanup_hung_fake_probe(env)


@pytest.mark.parametrize(
    ("assignment", "enabled"),
    [
        ("", False),
        ("BROWSER_ENABLED=false", False),
        ("BROWSER_ENABLED='false' # dormant", False),
        ("BROWSER_ENABLED=true", True),
        ("export BROWSER_ENABLED='true' # enabled", True),
        ("BROWSER_ENABLED=yes", True),
        ("BROWSER_ENABLED=true\nBROWSER_ENABLED=false", False),
    ],
)
def test_preflight_requires_browser_secret_only_when_literally_enabled(tmp_path, assignment, enabled):
    repo, env, _ = fake_deploy_host(tmp_path)
    env_file = repo / ".env"
    env_file.write_text(env_file.read_text() + assignment + "\n")
    before = env_file.read_bytes()
    result = run_fake_script(repo, env, "preflight.sh")
    assert (result.returncode != 0) is enabled, result.stdout + result.stderr
    assert ("missing secret file: browser_api_token" in result.stderr) is enabled
    assert env_file.read_bytes() == before


@pytest.mark.parametrize(
    ("mode", "uid", "content", "passed", "message"),
    [
        (0o400, 1000, "synthetic-browser-fixture", True, "secret browser_api_token mode 0400 uid 1000"),
        (0o600, 1000, "synthetic-browser-fixture", False, "browser_api_token mode must be 0400"),
        (0o400, 2000, "synthetic-browser-fixture", False, "browser_api_token uid must be 1000"),
        (0o400, 1000, "", False, "secret browser_api_token is empty"),
    ],
)
def test_preflight_browser_secret_permission_owner_and_nonempty_checks(
    tmp_path, mode, uid, content, passed, message
):
    repo, env, state = fake_deploy_host(tmp_path)
    env_file = repo / ".env"
    env_file.write_text(env_file.read_text() + "BROWSER_ENABLED=true\n")
    secret = repo / "secrets" / "browser_api_token"
    secret.write_text(content)
    secret.chmod(mode)
    state["browser_uid"] = uid
    update_fake_state(env, state)
    result = run_fake_script(repo, env, "preflight.sh")
    assert (result.returncode == 0) is passed, result.stdout + result.stderr
    assert message in result.stdout + result.stderr
    if content:
        assert content not in result.stdout + result.stderr


def test_preflight_reads_env_without_executing_shell_substitutions(tmp_path):
    repo, env, _ = fake_deploy_host(tmp_path)
    sentinel = repo / "must-not-be-created"
    env_file = repo / ".env"
    env_file.write_text(env_file.read_text() + f"BROWSER_ENABLED=$(touch {sentinel})\n")
    result = run_fake_script(repo, env, "preflight.sh")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not sentinel.exists()
    assert "missing secret file: browser_api_token" not in result.stderr


@pytest.mark.parametrize(
    ("browser", "health_code", "status"), [(False, 0, "SKIP"), (True, 0, "PASS"), (True, 1, "FAIL")]
)
def test_verify_browser_health_is_conditional_and_reports_failure(tmp_path, browser, health_code, status):
    repo, env, state = fake_deploy_host(tmp_path, browser=browser)
    state["browser_health_rc"] = health_code
    update_fake_state(env, state)
    result = run_fake_script(repo, env, "verify.sh")
    assert (result.returncode == 0) is (status != "FAIL"), result.stdout + result.stderr
    expected = f"[{status}] browser healthcheck" + (" (browser not running)" if not browser else "")
    assert expected + "\n" in result.stdout
    calls = browser_execs(env)
    assert len(calls) == int(browser)
    if browser:
        assert calls[0][-4:] == ["python", "-m", "browserd", "healthcheck"]
    # Without the relay the screen server is not looked for at all.
    assert "[SKIP] screen server (novnc not running)\n" in result.stdout


# --- M7: the screen in preflight, verify and isolation ---

@pytest.mark.parametrize(
    ("assignment", "enabled"),
    [("", False), ("SCREEN_ENABLED=false", False), ("SCREEN_ENABLED=true", True), ("export SCREEN_ENABLED='true'", True)],
)
def test_preflight_requires_the_screen_passwords_only_when_enabled(tmp_path, assignment, enabled):
    repo, env, _ = fake_deploy_host(tmp_path)
    env_file = repo / ".env"
    token = repo / "secrets" / "browser_api_token"
    token.write_text("synthetic-browser-fixture")
    token.chmod(0o400)
    env_file.write_text(env_file.read_text() + "BROWSER_ENABLED=true\n" + assignment + "\n")
    result = run_fake_script(repo, env, "preflight.sh")
    assert (result.returncode != 0) is enabled, result.stdout + result.stderr
    for name in ("vnc_password", "vnc_view_password"):
        assert (f"missing secret file: {name}" in result.stderr) is enabled
    # With the files in place (as `make secrets` leaves them) the same .env passes.
    for name, value in (("vnc_password", "Fu11pw9Z"), ("vnc_view_password", "V1ewpw7Q")):
        secret = repo / "secrets" / name
        secret.write_text(value)
        secret.chmod(0o400)
    result = run_fake_script(repo, env, "preflight.sh")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Fu11pw9Z" not in result.stdout + result.stderr and "V1ewpw7Q" not in result.stdout + result.stderr
    if enabled:
        assert "OK: secret vnc_password mode 0400 uid 1000" in result.stdout


def test_preflight_screen_needs_the_browser_and_strict_owner(tmp_path):
    repo, env, state = fake_deploy_host(tmp_path)
    for name in ("vnc_password", "vnc_view_password"):
        secret = repo / "secrets" / name
        secret.write_text("synthetic")
        secret.chmod(0o400)
    env_file = repo / ".env"
    original = env_file.read_text()
    env_file.write_text(original + "SCREEN_ENABLED=true\n")
    result = run_fake_script(repo, env, "preflight.sh")
    assert result.returncode != 0
    assert "SCREEN_ENABLED=true needs BROWSER_ENABLED=true" in result.stderr
    # A screen password owned by the wrong user is an error, not a warning.
    token = repo / "secrets" / "browser_api_token"
    token.write_text("synthetic-browser-fixture")
    token.chmod(0o400)
    env_file.write_text(original + "BROWSER_ENABLED=true\nSCREEN_ENABLED=true\n")
    state["vnc_uid"] = 0
    update_fake_state(env, state)
    result = run_fake_script(repo, env, "preflight.sh")
    assert result.returncode != 0 and "secret vnc_password uid must be 1000" in result.stderr


@pytest.mark.parametrize(
    ("codes", "status"),
    [
        ({"novnc/core/rfb.js": "401", "websockify": "401"}, "PASS"),  # the screen is on
        ({}, "PASS"),                                                  # off: core says 403 to both
        ({"novnc/core/rfb.js": "200", "websockify": "401"}, "FAIL"),  # a script got out without a login
        ({"novnc/core/rfb.js": "401", "websockify": "101"}, "FAIL"),  # a connection got through
        ({"novnc/core/rfb.js": "502", "websockify": "502"}, "FAIL"),  # Caddy passed it on to the relay
        ({"novnc/core/rfb.js": "", "websockify": ""}, "FAIL"),
    ],
)
def test_verify_screen_routes_must_refuse_a_visitor_without_a_login(tmp_path, codes, status):
    repo, env, state = fake_deploy_host(tmp_path)
    state["screen_http"] = codes
    update_fake_state(env, state)
    result = run_fake_script(repo, env, "verify.sh")
    assert (result.returncode == 0) is (status == "PASS"), result.stdout + result.stderr
    assert f"[{status}] screen routes " in result.stdout
    asked = [command["args"][-1] for command in fake_commands(env) if command["command"] == "curl" and "-w" in command["args"]]
    assert asked == ["https://fixture.invalid/screen/novnc/core/rfb.js", "https://fixture.invalid/screen/websockify"]


@pytest.mark.parametrize(
    ("screen_env", "health_code", "line"),
    [
        ("true", 0, "[PASS] screen server (x11vnc) listening in the browser container"),
        ("true", 1, "[FAIL] screen server (x11vnc) is not listening"),
        ("false", 0, "[SKIP] screen server (novnc runs, but SCREEN_ENABLED is not true for the browser)"),
    ],
)
def test_verify_checks_the_screen_server_when_the_relay_runs(tmp_path, screen_env, health_code, line):
    repo, env, state = fake_deploy_host(tmp_path, browser=True)
    state["services"]["novnc"] = "running"
    state["browser_screen_env"] = screen_env
    state["screen_health_rc"] = health_code
    update_fake_state(env, state)
    result = run_fake_script(repo, env, "verify.sh")
    assert (result.returncode == 0) is ("[FAIL]" not in line), result.stdout + result.stderr
    assert line in result.stdout
    asked = [args[-3:] for args in browser_execs(env)]
    assert (["browserd", "healthcheck", "screen"] in asked) is (screen_env == "true")


@pytest.mark.parametrize("mode", ["--ci", "--server"])
def test_isolation_probes_the_screen_relay_when_it_runs(tmp_path, mode):
    repo, env, state = fake_deploy_host(tmp_path, browser=True)
    state["services"]["novnc"] = "running"
    update_fake_state(env, state)
    result = run_fake_isolation(repo, env, mode)
    assert result.returncode == 0, result.stdout + result.stderr
    targets = {tuple(args[-2:]) for args in browser_execs(env)}
    assert {("10.77.5.30", "6080"), ("10.77.2.30", "6080")} <= targets
    sandbox_probes = " ".join(
        " ".join(command["args"]) for command in fake_commands(env)
        if command["command"] == "docker" and "exec" in command["args"] and "sandbox" in command["args"]
    )
    for url in ("http://10.77.5.40:5900", "http://10.77.2.30:6080/", "http://10.77.5.30:6080/"):
        assert url in sandbox_probes
    assert "SKIP sandbox to noVNC" not in result.stdout


def test_isolation_without_the_relay_says_so_and_still_probes_the_screen_server(tmp_path):
    repo, env, _ = fake_deploy_host(tmp_path, browser=True)
    result = run_fake_isolation(repo, env, "--server")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SKIP sandbox to noVNC: novnc service is not running\n" in result.stdout
    assert "ok fail: curl x11vnc\n" in result.stdout
    assert not {("10.77.5.30", "6080"), ("10.77.2.30", "6080")} & {tuple(args[-2:]) for args in browser_execs(env)}


def test_isolation_fails_if_the_browser_can_reach_the_relay(tmp_path):
    repo, env, state = fake_deploy_host(tmp_path, browser=True)
    state["services"]["novnc"] = "running"
    state["browser_probe_rc"] = 0  # the connection succeeded
    update_fake_state(env, state)
    result = run_fake_isolation(repo, env, "--ci")
    assert result.returncode != 0 and "FAIL isolation" in result.stderr
