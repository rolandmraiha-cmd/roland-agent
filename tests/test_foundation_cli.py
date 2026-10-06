import base64
import json
import os
import subprocess
import sys
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from conftest import make_config
from fastapi.testclient import TestClient

from agent import __main__ as cli
from agent.audit import Audit
from agent.backup import backup_now
from agent.locking import LockBusy, database_lock
from agent.web.app import create_app


def command(tmp_path, *arguments, **environment):
    root = Path(__file__).resolve().parents[1]
    return subprocess.run(
        [sys.executable, "-m", "agent", *arguments],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PYTHONPATH": str(root),
            "DATA_DIR": str(tmp_path),
            "MODEL_PROVIDER": "invalid",
            **environment,
        },
    )


def test_backup_and_restore_cli_work_without_a_model(tmp_path, make_agent):
    agent = make_agent(backup_dir=tmp_path / "backups", backup_workspace=False)
    agent.memory.remember("CLI snapshot")
    agent.memory.close()
    response = command(tmp_path, "backup-now", BACKUP_DIR=str(tmp_path / "backups"), BACKUP_WORKSPACE="false")
    assert response.returncode == 0, response.stderr
    snapshot = json.loads(response.stdout)["files"][0]
    response = command(tmp_path, "restore", snapshot)
    assert response.returncode == 0, response.stderr
    response = command(tmp_path, "audit-verify")
    assert response.returncode == 0 and json.loads(response.stdout)["ok"]


def test_restore_cli_refuses_a_live_server_lock(tmp_path, make_agent):
    agent = make_agent(backup_dir=tmp_path / "backups", backup_workspace=False)
    snapshot = backup_now(agent.config, agent.memory, agent.audit)[0]
    agent.memory.close()
    original = (tmp_path / "agent.db").read_bytes()
    with database_lock(tmp_path):
        result = command(tmp_path, "restore", str(snapshot))
    assert result.returncode != 0 and "in use" in result.stderr
    assert (tmp_path / "agent.db").read_bytes() == original


def test_serve_holds_lock_before_build_and_releases_it_on_failure(tmp_path, monkeypatch, make_agent):
    agent = make_agent()
    monkeypatch.setattr(cli.Config, "from_env", lambda: agent.config)

    def build(*, config, validate):
        assert validate
        with pytest.raises(LockBusy):
            with database_lock(tmp_path, exclusive=True):
                pass
        return agent

    def fail(agent):
        raise RuntimeError("Server stopped")

    monkeypatch.setattr(cli, "build", build)
    monkeypatch.setattr(cli, "serve", fail)
    monkeypatch.setattr(sys, "argv", ["agent", "serve"])
    with pytest.raises(RuntimeError, match="Server stopped"):
        cli.main()
    with database_lock(tmp_path, exclusive=True):
        pass


def test_gen_token_needs_no_configuration_and_encodes_32_random_bytes(tmp_path):
    result = command(tmp_path, "gen-token", AGENT_PASSWORD_HASH_FILE=str(tmp_path / "missing-secret"))
    assert result.returncode == 0
    assert len(base64.urlsafe_b64decode(result.stdout.strip() + "=")) == 32
    assert not (tmp_path / "agent.db").exists()


@pytest.mark.parametrize("arguments", [("restore",), ("backup-now", "extra"), ("healthcheck", "--check")])
def test_invalid_cli_arguments_fail_without_creating_a_database(tmp_path, arguments):
    assert command(tmp_path, *arguments).returncode != 0
    assert not (tmp_path / "agent.db").exists()


def test_backup_now_without_database_never_creates_one(tmp_path):
    result = command(tmp_path, "backup-now", BACKUP_DIR=str(tmp_path / "backups"), BACKUP_WORKSPACE="false")
    assert result.returncode != 0 and "No existing database" in result.stderr
    assert not (tmp_path / "agent.db").exists()


def test_health_endpoint_is_minimal_public_and_respects_host_checks(make_agent):
    agent = make_agent(allowed_hosts=("agent.example",))
    with TestClient(create_app(agent, run_scheduler=False)) as client:
        assert client.get("/healthz").status_code == 400
        response = client.get("/healthz", headers={"Host": "agent.example"})
        assert response.status_code == 200 and response.json() == {"ok": True}
        assert client.get("/api/status", headers={"Host": "agent.example"}).status_code == 401


@pytest.mark.parametrize(
    "status,payload",
    [
        (200, {"ok": True}),
        (200, {"ok": 1}),
        (200, {"ok": True, "extra": 1}),
        (503, {"ok": True}),
        (302, {"ok": True}),
    ],
)
def test_healthcheck_uses_bound_host_no_proxy_and_no_redirects(tmp_path, status, payload, monkeypatch):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers["Host"]))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Location", "http://untrusted.invalid/healthz")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    config = make_config(tmp_path, host="0.0.0.0", port=server.server_port, agent_host="agent.example")
    try:
        assert cli.healthcheck(config) == (status == 200 and payload.get("ok") is True and len(payload) == 1)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    assert seen == [("/healthz", "agent.example")]


def test_invalid_health_target_is_refused_without_model_or_db(tmp_path):
    assert not cli.healthcheck(make_config(tmp_path, host="remote.invalid"))
    assert not (tmp_path / "agent.db").exists()


def test_audit_corruption_blocks_restore(tmp_path, make_agent):
    from agent.backup import restore
    from agent.migrations.backup import database_snapshot

    agent = make_agent()
    Audit(agent.memory).write("system", "startup")
    agent.memory._exec("DROP TRIGGER audit_no_update")
    agent.memory._exec("UPDATE audit_log SET event='tampered'")
    snapshot = database_snapshot(agent.memory._db, tmp_path / "tampered.db.gz")
    agent.memory.close()
    original = agent.config.db_path.read_bytes()
    with pytest.raises(ValueError, match="audit verification"):
        restore(agent.config, snapshot)
    assert agent.config.db_path.read_bytes() == original


@pytest.mark.parametrize(
    "settings",
    [
        {"backup_keep_daily": 0},
        {"backup_keep_weekly": -1},
        {"backup_workspace_keep": 0},
        {"backup_workspace_max_mb": -1},
        {"backup_time": "24:00"},
        {"backup_time": "3:30"},
    ],
)
def test_bad_backup_settings_fail_before_snapshot(tmp_path, make_agent, settings):
    from agent.backup import validate_backup_config

    config = make_agent(backup_dir=tmp_path / "backups").config
    with pytest.raises(ValueError):
        validate_backup_config(replace(config, **settings))
    assert not (tmp_path / "backups").exists()


def test_backup_destination_must_be_outside_workspace(tmp_path, make_agent):
    from agent.backup import validate_backup_config

    config = make_agent().config
    with pytest.raises(ValueError, match="outside"):
        validate_backup_config(replace(config, backup_dir=config.workspace / "backups"))
