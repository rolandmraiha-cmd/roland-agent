"""Syntax and policy checks for M2.5/M2.6 deploy scripts (no live host required)."""

from __future__ import annotations

import os
import re
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
