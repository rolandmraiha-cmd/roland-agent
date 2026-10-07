"""Exercise the real Caddy/core containers without invoking model inference."""

import os
import subprocess
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]


def main(project: str):
    if os.getenv("GITHUB_ACTIONS") != "true" or not project.startswith("roland-agent-edge-ci-"):
        raise SystemExit("The live edge probe is restricted to its disposable CI project")
    compose = ["docker", "compose", "--project-name", project, "-f", str(ROOT / "docker-compose.yml")]
    password = (ROOT / ".ci-workspace/login-password").read_text()
    # Only the localhost test CA is untrusted; production uses Caddy's ACME certificates.
    with httpx.Client(
        base_url="https://localhost", verify=False, trust_env=False, follow_redirects=False, timeout=15
    ) as client:
        response = client.get("/login")
        assert response.status_code == 200
        assert response.headers["Strict-Transport-Security"] == "max-age=31536000"
        assert "Server" not in response.headers and "Via" not in response.headers
        assert "unsafe-inline" not in response.headers["Content-Security-Policy"]
        assert client.get("/api/status").status_code == 401
        for path in (
            "/healthz",
            "/healthz/",
            "/internal",
            "/internal/screen-auth",
            "/%69nternal/screen-auth",
        ):
            assert client.get(path).status_code == 404, path
        redirect = client.get("http://localhost/login")
        assert (
            redirect.status_code in {301, 308} and redirect.headers["Location"] == "https://localhost/login"
        )
        assert client.post("/login", data={"password": password}).status_code == 403
        response = client.post(
            "/login",
            data={"password": password},
            headers={
                "Origin": "https://localhost",
                "X-Forwarded-For": "8.8.8.8",
                "X-Forwarded-Proto": "http",
            },
        )
        assert response.status_code == 303
        cookie = response.headers["Set-Cookie"]
        assert cookie.startswith("__Host-agent_session=")
        assert all(value in cookie for value in ("HttpOnly", "Secure", "SameSite=strict", "Path=/"))
        assert "Domain=" not in cookie
        status = client.get("/api/status")
        assert status.status_code == 200 and status.json()["shell"] is True
        headers = {"Origin": "https://localhost", "X-CSRF-Token": status.json()["csrf"]}
        assert client.post("/api/chats", headers={"Origin": "https://localhost"}).status_code == 403
        response = client.post("/api/chats", headers=headers)
        assert response.status_code == 200
        chat_id = response.json()["id"]
        for path in ("/screen/websockify", "/screen/novnc/vnc.html"):
            assert client.get(path).status_code == 403, path
        # State and sessions must survive a real process/container restart.
        subprocess.run(compose + ["restart", "core"], check=True, timeout=90)
        subprocess.run(compose + ["up", "-d", "--wait", "--wait-timeout", "180"], check=True, timeout=210)
        assert client.get("/api/status").status_code == 200
        assert any(chat["id"] == chat_id for chat in client.get("/api/chats").json())
        assert client.post("/logout", headers=headers).status_code == 200
        assert client.get("/api/status").status_code == 401
    checks = """
import json, os, pathlib, socket, sqlite3
assert os.geteuid() == 1000
status = pathlib.Path('/proc/self/status').read_text()
assert 'CapEff:\\t0000000000000000' in status and 'NoNewPrivs:\\t1' in status
for folder in ('/data', '/backups', '/workspace'):
    path = pathlib.Path(folder) / 'ci-write-check'
    path.write_text('synthetic')
    path.unlink()
with sqlite3.connect('/data/agent.db') as db:
    detail = db.execute("SELECT detail FROM audit_log WHERE event='login_ok' ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert json.loads(detail)['client'] != '8.8.8.8'
try:
    socket.create_connection(('10.77.6.10', 8080), 2)
except OSError:
    pass
else:
    raise AssertionError('Core is listening on its model interface')
"""
    subprocess.run(compose + ["exec", "-T", "core", "python", "-c", checks], check=True, timeout=30)
    print(
        "HTTPS, auth, peer forwarding, CSRF, disabled screen, restart persistence and runtime isolation passed."
    )


if __name__ == "__main__":
    main(sys.argv[1])
