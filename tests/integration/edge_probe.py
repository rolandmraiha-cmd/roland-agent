"""Exercise the real Caddy/core containers without invoking model inference."""

import os
import subprocess
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]


SCREEN_PATHS = ("/screen/novnc/core/rfb.js", "/screen/novnc/vnc.html", "/screen/websockify")

# Run inside the relay's own container: what it serves, and as whom.
RELAY_CHECKS = """
import os, urllib.error, urllib.request
assert os.geteuid() == 1000
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open('http://10.77.2.30:6080/core/rfb.js', timeout=5) as response:
    assert response.status == 200 and 'javascript' in response.headers['Content-Type']
    assert b'export default class RFB' in response.read()
for path in ('/', '/core/', '/vnc.html', '/vnc_lite.html', '/package.json', '/app/ui.js'):
    try:
        opener.open('http://10.77.2.30:6080' + path, timeout=5)
    except urllib.error.HTTPError as error:
        assert error.code == 404, (path, error.code)
    else:
        raise AssertionError('the relay serves ' + path)
"""

# Run inside another container: the relay must be out of reach from there.
NO_WAY_TO_RELAY = """
import socket
for address in ('10.77.2.30', '10.77.5.30'):
    try:
        socket.create_connection((address, 6080), 3).close()
    except OSError:
        continue
    raise AssertionError('reached the screen relay at ' + address)
"""


def screen_probe(project: str, compose: list[str], password: str) -> None:
    """A7.3, the part that needs no browser: with the screen switched on, the relay is only
    reachable through Caddy, and Caddy lets nobody through whom core has not approved."""
    with httpx.Client(
        base_url="https://localhost", verify=False, trust_env=False, follow_redirects=False, timeout=15
    ) as client:
        # Without a login: 401 on every screen path, a websocket handshake included.
        upgrade = {"Connection": "Upgrade", "Upgrade": "websocket", "Sec-WebSocket-Version": "13",
                   "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==", "Origin": "https://localhost"}
        for path in SCREEN_PATHS:
            assert client.get(path).status_code == 401, path
        assert client.get("/screen/websockify", headers=upgrade).status_code == 401
        assert client.get("/screen").status_code in {302, 303, 401}  # the page itself needs the login too
        response = client.post("/login", data={"password": password}, headers={"Origin": "https://localhost"})
        assert response.status_code == 303
        status = client.get("/api/status").json()
        assert status["screen"] == {"enabled": True}
        headers = {"Origin": "https://localhost", "X-CSRF-Token": status["csrf"]}
        # Logged in, but with no screen session open: still nothing.
        for path in SCREEN_PATHS:
            assert client.get(path).status_code == 403, path
        assert client.get("/screen/websockify", headers=upgrade).status_code == 403
        # The script route never carries a websocket, and takes nothing but GET.
        assert client.get("/screen/novnc/websockify", headers=upgrade).status_code == 403
        assert client.post("/screen/novnc/core/rfb.js", headers=headers).status_code == 405
        # No browser runs in this stack, so a session can't start, and a failed start leaves
        # nothing behind that would open the routes. The answer holds no password.
        for mode in ("watch", "control"):
            # Core gives the missing browser a while to answer before it says so.
            answer = client.post("/api/screen/session", json={"mode": mode}, headers=headers, timeout=90)
            assert answer.status_code == 503 and "vnc_password" not in answer.text, answer.status_code
        for path in SCREEN_PATHS:
            assert client.get(path).status_code == 403, path
        assert client.get("/screen").status_code == 200  # the page is served; it will say it can't connect
        assert client.post("/logout", headers=headers).status_code == 200
        for path in SCREEN_PATHS:
            assert client.get(path).status_code == 401, path
    subprocess.run(compose + ["exec", "-T", "novnc", "python", "-c", RELAY_CHECKS], check=True, timeout=60)
    for service in ("core", "sandbox"):
        subprocess.run(compose + ["exec", "-T", service, "python", "-c", NO_WAY_TO_RELAY], check=True, timeout=60)
    print("Screen routes: 401 without a login, 403 without a screen session, relay unreachable around Caddy.")


def main(project: str, screen: bool = False):
    if os.getenv("GITHUB_ACTIONS") != "true" or not project.startswith("roland-agent-edge-ci-"):
        raise SystemExit("The live edge probe is restricted to its disposable CI project")
    compose = ["docker", "compose", "--project-name", project, "-f", str(ROOT / "docker-compose.yml")]
    password = (ROOT / ".ci-workspace/login-password").read_text()
    if screen:
        screen_probe(project, compose, password)
        return
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
    main(sys.argv[1], screen=sys.argv[2:] == ["--screen"])
