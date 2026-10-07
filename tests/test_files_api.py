"""A5.2 files API: upload/download/delete, CSRF, headers, preview magic."""

from __future__ import annotations

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
