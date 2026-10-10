"""A5.2 files API: upload/download/delete, CSRF, headers, preview magic."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap

import h11
import pytest
from fastapi.testclient import TestClient

from agent.web.app import create_app

PASSWORD = "correct horse battery staple"


def login(client, origin="https://agent.test"):
    response = client.post(
        "/login", data={"password": PASSWORD}, headers={"Origin": origin}, follow_redirects=False
    )
    assert response.status_code == 303
    client.headers["X-CSRF-Token"] = client.get("/api/status").json()["csrf"]
    client.headers["Origin"] = origin
    return response


@pytest.fixture
def client(make_agent):
    agent = make_agent()
    agent.config.workspace.mkdir(parents=True, exist_ok=True)
    app = create_app(agent, run_scheduler=False)
    with TestClient(app, base_url="https://agent.test") as test_client:
        login(test_client)
        test_client.agent = agent
        yield test_client


def test_upload_download_delete_roundtrip(client):
    put = client.put(
        "/api/files/content?path=docs/hello.txt",
        content=b"hello world",
        headers={"X-Overwrite": "0"},
    )
    assert put.status_code == 201, put.text
    body = put.json()
    assert body["path"] == "docs/hello.txt"
    assert body["size"] == 11
    assert len(body["sha256"]) == 64

    listing = client.get("/api/files", params={"path": "docs"}).json()
    names = [e["name"] for e in listing["entries"]]
    assert "hello.txt" in names

    got = client.get("/api/files/download", params={"path": "docs/hello.txt"})
    assert got.status_code == 200
    assert got.content == b"hello world"
    assert got.headers["content-disposition"].startswith("attachment")
    assert got.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in got.headers["content-security-policy"]
    assert got.headers["content-type"].startswith("application/octet-stream")

    deleted = client.delete("/api/files", params={"path": "docs/hello.txt"})
    assert deleted.status_code == 200
    assert client.get("/api/files/download", params={"path": "docs/hello.txt"}).status_code == 404
    trash = client.get("/api/trash").json()
    assert any(t["original_path"] == "docs/hello.txt" for t in trash)


def test_upload_too_large_413(client, monkeypatch):
    from agent.web import routes_files

    real = routes_files.workspace_for

    def tiny(agent):
        ws = real(agent)
        ws.upload_max_mb = 0  # 0 MiB → any byte is too large; use fractional via monkeypatch method
        return ws

    # 1 byte max via upload_max_bytes override
    monkeypatch.setattr(routes_files, "workspace_for", tiny)

    class TinyWS:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def upload_max_bytes(self):
            return 8

    def wrap(agent):
        return TinyWS(real(agent))

    monkeypatch.setattr(routes_files, "workspace_for", wrap)
    # Stream more than 8 bytes without relying on Content-Length alone.
    payload = b"0123456789abcdef"
    response = client.put(
        "/api/files/content?path=big.bin",
        content=payload,
        headers={"X-Overwrite": "0", "Content-Length": str(len(payload))},
    )
    assert response.status_code == 413


def test_upload_requires_csrf(make_agent):
    agent = make_agent()
    agent.config.workspace.mkdir(parents=True, exist_ok=True)
    app = create_app(agent, run_scheduler=False)
    with TestClient(app, base_url="https://agent.test") as client:
        login(client)
        # Drop CSRF
        client.headers.pop("X-CSRF-Token", None)
        response = client.put(
            "/api/files/content?path=x.txt",
            content=b"x",
            headers={"Origin": "https://agent.test"},
        )
        assert response.status_code == 403


def test_download_headers_attachment_nosniff_csp(client):
    client.put("/api/files/content?path=f.txt", content=b"data", headers={"X-Overwrite": "1"})
    response = client.get("/api/files/download", params={"path": "f.txt"})
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert "sandbox" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("size", [0, 3 * 64 * 1024 + 17])
def test_download_returns_complete_binary_and_empty_files(client, size):
    data = (bytes(range(256)) * ((size + 255) // 256))[:size]
    (client.agent.config.workspace / "chunks.bin").write_bytes(data)
    response = client.get("/api/files/download", params={"path": "chunks.bin"})
    assert response.status_code == 200
    assert response.content == data


async def test_download_truncation_finishes_valid_http_response(make_agent, monkeypatch):
    from agent.web import routes_files

    agent = make_agent()
    ws = routes_files.workspace_for(agent)
    ws.ensure()
    path = ws.root / "mutable.bin"
    path.write_bytes(b"x" * (3 * 64 * 1024))
    monkeypatch.setattr(routes_files, "workspace_for", lambda _: ws)
    router = routes_files.build_router(agent)
    download = next(r.endpoint for r in router.routes if r.path == "/api/files/download")
    response = await download("mutable.bin")
    protocol = h11.Connection(h11.SERVER)
    protocol.receive_data(b"GET / HTTP/1.1\r\nHost: agent.test\r\n\r\n")
    assert isinstance(protocol.next_event(), h11.Request)
    assert isinstance(protocol.next_event(), h11.EndOfMessage)
    truncated = False
    finished = False

    async def send(message):
        nonlocal truncated, finished
        if message["type"] == "http.response.start":
            protocol.send(h11.Response(status_code=message["status"], headers=message["headers"]))
        elif message["type"] == "http.response.body":
            protocol.send(h11.Data(data=message.get("body", b"")))
            if message.get("body") and not truncated:
                with path.open("wb"):
                    pass
                truncated = True
            if not message.get("more_body", False):
                protocol.send(h11.EndOfMessage())
                finished = True

    async def receive():
        return {"type": "http.disconnect"}

    await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert truncated and finished


def test_preview_only_real_images(client):
    # .png name with HTML body → 415
    client.put(
        "/api/files/content?path=evil.png",
        content=b"<html><script>alert(1)</script></html>",
        headers={"X-Overwrite": "1"},
    )
    assert client.get("/api/files/preview", params={"path": "evil.png"}).status_code == 415

    png = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
        b"\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    client.put("/api/files/content?path=ok.png", content=png, headers={"X-Overwrite": "1"})
    preview = client.get("/api/files/preview", params={"path": "ok.png"})
    assert preview.status_code == 200
    assert preview.headers["content-type"].startswith("image/png")
    assert "sandbox" in preview.headers["content-security-policy"]


def test_filename_with_quotes_and_unicode_content_disposition(client):
    name = 'report "Q1" café.txt'
    client.put(
        f"/api/files/content?path={name}",
        content=b"x",
        headers={"X-Overwrite": "1"},
    )
    response = client.get("/api/files/download", params={"path": name})
    assert response.status_code == 200
    cd = response.headers["content-disposition"]
    assert "attachment" in cd
    assert "filename*=" in cd
    assert "UTF-8''" in cd
    assert '"' not in cd.split("filename*=")[0].split("filename=")[-1] or "filename*=UTF-8" in cd


def test_listing_shows_symlinks_unfollowed(client):
    root = client.agent.config.workspace
    (root / "real.txt").write_text("hi")
    (root / "link.txt").symlink_to(root / "real.txt")
    listing = client.get("/api/files").json()
    links = [e for e in listing["entries"] if e["type"] == "symlink"]
    assert links
    assert any(e["name"].endswith("@") for e in links)
    # Download via symlink path must fail (not followed).
    assert client.get("/api/files/download", params={"path": "link.txt"}).status_code == 400


def test_upload_refuses_symlink_dest(client):
    root = client.agent.config.workspace
    (root / "link.txt").symlink_to(root / "missing-target")
    response = client.put(
        "/api/files/content?path=link.txt",
        content=b"hijack",
        headers={"X-Overwrite": "1"},
    )
    assert response.status_code == 400, response.text
    detail = response.json()["detail"].lower()
    assert "outside" in detail or "symbolic" in detail
    assert (root / "link.txt").is_symlink()


@pytest.mark.parametrize("spec_version", ["2.0", "2.4"])
async def test_download_closes_reader_when_client_disconnects(make_agent, monkeypatch, spec_version):
    from starlette.requests import ClientDisconnect

    from agent.web import routes_files

    agent = make_agent()
    ws = routes_files.workspace_for(agent)
    ws.ensure()
    (ws.root / "chunks.bin").write_bytes(b"x" * (3 * 64 * 1024))
    opened = []
    real_open = ws.open_read

    def track_open(path):
        handle = real_open(path)
        opened.append(handle)
        return handle

    monkeypatch.setattr(ws, "open_read", track_open)
    monkeypatch.setattr(routes_files, "workspace_for", lambda _: ws)
    router = routes_files.build_router(agent)
    download = next(r.endpoint for r in router.routes if r.path == "/api/files/download")
    response = await download("chunks.bin")
    disconnected = asyncio.Event()

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            if spec_version == "2.4":
                raise OSError("client disconnected")
            disconnected.set()

    async def receive():
        await disconnected.wait()
        return {"type": "http.disconnect"}

    scope = {"type": "http", "asgi": {"spec_version": spec_version}}
    if spec_version == "2.4":
        with pytest.raises(ClientDisconnect):
            await response(scope, receive, send)
    else:
        await response(scope, receive, send)
    assert opened and all(handle.closed for handle in opened)


@pytest.mark.skipif(sys.platform != "linux", reason="matches the Linux core memory limit")
def test_large_download_and_hash_fit_below_core_memory_limit(tmp_path):
    # A sparse file larger than core's 640 MiB cap; the child cannot buffer it.
    root = tmp_path / "large-workspace"
    root.mkdir()
    size = 768 * 1024 * 1024
    with (root / "large.bin").open("wb") as handle:
        handle.truncate(size)
    probe = textwrap.dedent(
        """
        import asyncio
        import resource
        import sys
        from pathlib import Path
        from types import SimpleNamespace
        from agent.web.routes_files import build_router
        from agent.workspace import Workspace

        root = Path(sys.argv[1])
        config = SimpleNamespace(workspace=root, workspace_quota_mb=8192,
            workspace_reserve_mb=1, workspace_max_files=50000,
            upload_max_mb=100, trash_keep_days=7)
        router = build_router(SimpleNamespace(config=config, memory=None))
        download = next(r.endpoint for r in router.routes if r.path == '/api/files/download')
        resource.setrlimit(resource.RLIMIT_AS, (192 * 1024 * 1024, 192 * 1024 * 1024))

        async def run():
            response = await download('large.bin')
            assert 'content-length' not in response.headers
            # Discard chunks and disconnect early; never collect the response in a client.
            disconnected = asyncio.Event()
            count = 0
            async def send(message):
                nonlocal count
                if message['type'] == 'http.response.body' and message.get('body'):
                    assert len(message['body']) <= 64 * 1024
                    count += 1
                    if count == 2:
                        disconnected.set()
            async def receive():
                await disconnected.wait()
                return {'type': 'http.disconnect'}
            await response({'type': 'http', 'asgi': {'spec_version': '2.0'}}, receive, send)
            assert count >= 2
            # file_info uses the same bounded hash reader on an unindexed large file.
            info = Workspace(root).info('large.bin')
            assert info['size'] == 768 * 1024 * 1024
            assert len(info['sha256']) == 64

        asyncio.run(run())
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe, str(root)], capture_output=True, text=True, timeout=30
    )
    assert completed.returncode == 0, completed.stderr
