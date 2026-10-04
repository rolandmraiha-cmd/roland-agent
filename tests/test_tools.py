import pytest

from agent.memory import Memory
from agent.tools import MAX_OUTPUT, ToolContext, call_tool


@pytest.fixture
def ctx(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ToolContext(Memory(tmp_path / "m.db"), ws, "Europe/Helsinki", allow_shell=True)


@pytest.mark.asyncio
async def test_files_stay_in_workspace(ctx, tmp_path):
    (tmp_path / "secret.txt").write_text("nope")
    assert "outside" in await call_tool(ctx, "read_file", {"path": "../secret.txt"})
    assert "outside" in await call_tool(ctx, "read_file", {"path": "/etc/passwd"})
    assert "outside" in await call_tool(ctx, "write_file", {"path": "../x", "content": "x"})
    (ctx.workspace / "link").symlink_to(tmp_path / "secret.txt")
    assert "outside" in await call_tool(ctx, "read_file", {"path": "link"})
    await call_tool(ctx, "write_file", {"path": "a/b.txt", "content": "hi"})
    assert await call_tool(ctx, "read_file", {"path": "a/b.txt"}) == "hi"
    assert "a/" in await call_tool(ctx, "list_files", {})


@pytest.mark.asyncio
async def test_shell_runs_in_workspace_without_secrets(ctx, monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "topsecret")
    out = await call_tool(ctx, "run_shell", {"command": "pwd; echo pw=$MODEL_API_KEY"})
    assert str(ctx.workspace) in out and "topsecret" not in out and "exit code 0" in out


@pytest.mark.asyncio
async def test_shell_off_by_default(ctx, make_agent, monkeypatch):
    monkeypatch.delenv("ALLOW_SHELL", raising=False)
    monkeypatch.setenv("AGENT_IN_CONTAINER", "1")  # being in a container no longer turns it on
    assert make_agent().allow_shell is False
    monkeypatch.setenv("ALLOW_SHELL", "true")
    assert make_agent().allow_shell is True
    ctx.allow_shell = False
    assert "turned off" in await call_tool(ctx, "run_shell", {"command": "ls"})


@pytest.mark.asyncio
async def test_shell_output_is_capped(ctx):
    out = await call_tool(ctx, "run_shell", {"command": "yes hello"})
    assert "stopped" in out and len(out) < MAX_OUTPUT + 500


@pytest.mark.asyncio
async def test_fetch_blocks_private_addresses(ctx):
    for url in ["http://127.0.0.1:8080/", "http://localhost/", "http://169.254.169.254/latest",
                "http://10.0.0.1/", "http://[::1]/", "http://[::ffff:127.0.0.1]/",
                "http://[fe80::1]/", "http://192.168.1.1/", "file:///etc/passwd",
                "http://[64:ff9b::7f00:1]/", "http://[2002:7f00:1::]/", "http://[64:ff9b:1::a00:1]/"]:
        out = await call_tool(ctx, "fetch_url", {"url": url})
        assert out.startswith("Error"), url


@pytest.mark.asyncio
async def test_symlink_out_of_workspace_refused(ctx, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")
    (ctx.workspace / "link").symlink_to(secret)
    (ctx.workspace / "dirlink").symlink_to(tmp_path)
    assert "outside" in await call_tool(ctx, "read_file", {"path": "link"})
    assert "outside" in await call_tool(ctx, "read_file", {"path": "dirlink/secret.txt"})
    assert "outside" in await call_tool(ctx, "write_file", {"path": "link", "content": "x"})
    assert secret.read_text() == "top secret"


def test_fetch_connects_to_checked_address(monkeypatch):
    """DNS rebinding: the request goes to the IP that passed the check, with the real hostname
    kept for the Host header and the TLS certificate check."""
    from urllib.parse import urlparse
    from agent import tools
    monkeypatch.setattr(tools.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.215.14", 0))])
    assert tools._public_ip("example.com") == "93.184.215.14"
    url, headers, ext = tools._pinned(urlparse("https://example.com:8443/a?b=1"), "93.184.215.14")
    assert url == "https://93.184.215.14:8443/a?b=1"
    assert headers == {"Host": "example.com:8443"} and ext == {"sni_hostname": "example.com"}
    url, _, ext = tools._pinned(urlparse("http://example.com/"), "2606:2800::1")
    assert url == "http://[2606:2800::1]/" and ext == {}
    monkeypatch.setattr(tools.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.215.14", 0)), (2, 1, 6, "", ("10.0.0.5", 0))])
    assert tools._public_ip("rebind.example") is None


@pytest.mark.asyncio
async def test_jobs_tools(ctx):
    assert "not a valid" in await call_tool(ctx, "schedule_job", {"name": "x", "cron": "bad", "prompt": "p"})
    out = await call_tool(ctx, "schedule_job", {"name": "News", "cron": "0 7 * * *", "prompt": "news"})
    assert "job 1" in out and "approval" in out
    job = ctx.memory.job(1)
    assert job.approved is False and job.enabled is False
    assert "waiting for approval" in await call_tool(ctx, "list_jobs", {})
    assert ctx.memory.job(1).origin == "agent"
    out = await call_tool(ctx, "schedule_job", {"name": "Big", "cron": "0 7 * * *", "prompt": "x" * 5001})
    assert "at most 5000" in out and ctx.memory.job(2) is None
    assert "deleted" in await call_tool(ctx, "cancel_job", {"job_id": 1})
    assert "no tool" in await call_tool(ctx, "nope", {})
