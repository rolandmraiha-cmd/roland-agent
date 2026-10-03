import pytest

from agent.memory import Memory
from agent.tools import ToolContext, call_tool


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
async def test_shell_off_outside_container(ctx):
    ctx.allow_shell = False
    assert "turned off" in await call_tool(ctx, "run_shell", {"command": "ls"})


@pytest.mark.asyncio
async def test_fetch_blocks_private_addresses(ctx):
    for url in ["http://127.0.0.1:8080/", "http://localhost/", "http://169.254.169.254/latest",
                "http://10.0.0.1/", "http://[::1]/", "http://[::ffff:127.0.0.1]/",
                "http://[fe80::1]/", "http://192.168.1.1/", "file:///etc/passwd"]:
        out = await call_tool(ctx, "fetch_url", {"url": url})
        assert out.startswith("Error"), url


@pytest.mark.asyncio
async def test_jobs_tools(ctx):
    assert "not a valid" in await call_tool(ctx, "schedule_job", {"name": "x", "cron": "bad", "prompt": "p"})
    assert "Scheduled job 1" in await call_tool(ctx, "schedule_job", {"name": "News", "cron": "0 7 * * *", "prompt": "news"})
    assert "News" in await call_tool(ctx, "list_jobs", {})
    assert "deleted" in await call_tool(ctx, "cancel_job", {"job_id": 1})
    assert "no tool" in await call_tool(ctx, "nope", {})
