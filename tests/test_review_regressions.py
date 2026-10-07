import asyncio
from types import SimpleNamespace

import httpx
import pytest

from agent.brain import ToolCall
from agent.models.llamacpp import LlamaCppBrain
from agent.tools import MAX_OUTPUT, ToolContext, call_tool
from tests.conftest import call


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", ['{"content":"oops",', "[]", "null", "42"])
async def test_invalid_arguments_do_not_write_files(make_agent, arguments):
    agent = make_agent([("", [call("write_file", arguments)]), "corrected"])
    events = [event async for event in agent.chat(agent.memory.new_chat(), "go")]
    assert not list(agent.config.workspace.iterdir())
    assert "Error: Tool arguments" in agent.brain.seen[1][-1]["content"]
    assert not any(event["type"] == "tool" for event in events)


def test_valid_empty_arguments_remain_supported():
    assert ToolCall("c", "list_files", "").args() == {}


@pytest.mark.asyncio
async def test_large_file_read_is_bounded(make_agent, monkeypatch):
    agent = make_agent()
    path = agent.config.workspace / "large.txt"
    path.write_text("x" * (MAX_OUTPUT * 20))
    from pathlib import Path

    original = Path.open
    reads = []

    class CheckedReader:
        def __init__(self, file):
            self.file = file

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.file.close()

        def read(self, size=-1):
            reads.append(size)
            assert size == MAX_OUTPUT + 1
            return self.file.read(size)

    monkeypatch.setattr(Path, "open", lambda self, *a, **kw: CheckedReader(original(self, *a, **kw)))
    result = await call_tool(agent.ctx, "read_file", {"path": "large.txt"})
    assert reads == [MAX_OUTPUT + 1]
    assert result.startswith("x" * MAX_OUTPUT) and "file continues" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("during_cleanup", [False, True])
async def test_shell_cancellation_kills_process_group(make_agent, monkeypatch, during_cleanup):
    from agent import local_shell

    started = asyncio.Event()
    killed = []

    async def read(size):
        if during_cleanup:
            return b""
        started.set()
        await asyncio.Event().wait()

    async def communicate():
        if during_cleanup and not killed:
            started.set()
            await asyncio.Event().wait()
        return b"", b""

    proc = SimpleNamespace(pid=12345, stdout=SimpleNamespace(read=read), communicate=communicate)

    async def spawn(*args, **kwargs):
        assert kwargs["start_new_session"] is True
        return proc

    monkeypatch.setattr(local_shell.asyncio, "create_subprocess_shell", spawn)
    monkeypatch.setattr(local_shell.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    agent = make_agent()
    ctx = ToolContext(agent.memory, agent.config.workspace, agent.config.timezone, True)
    task = asyncio.create_task(call_tool(ctx, "run_shell", {"command": "sleep 60"}))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert killed == [(12345, 9)]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_model_response_closed_on_completion_or_error(failure):
    closed = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/props"):
            return httpx.Response(200, json={})
        if failure:
            raise RuntimeError("stream failed")
        chunk = {
            "choices": [
                {
                    "delta": {"content": '{"action":"reply","text":"hello"}'},
                    "finish_reason": None,
                }
            ]
        }
        import json as _json

        body = f"data: {_json.dumps(chunk)}\n\ndata: [DONE]\n\n"
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

    class TrackingTransport(httpx.MockTransport):
        async def handle_async_request(self, request):
            try:
                return await super().handle_async_request(request)
            finally:
                if str(request.url.path).endswith("/chat/completions"):
                    closed.append(True)

    brain = LlamaCppBrain("http://127.0.0.1:8080", "test", "tok")
    await brain._client.aclose()
    brain._client = httpx.AsyncClient(transport=TrackingTransport(handler))
    brain._supports_tool_role = False
    try:
        if failure:
            with pytest.raises(RuntimeError):
                _ = [item async for item in brain.stream([{"role": "user", "content": "hi"}], [])]
        else:
            items = [item async for item in brain.stream([{"role": "user", "content": "hi"}], [])]
            assert any(getattr(item, "text", None) == "hello" for item in items)
    finally:
        await brain.aclose()
    assert closed == [True]


@pytest.mark.asyncio
async def test_modified_tool_name_cannot_dispatch_an_action(make_agent):
    agent = make_agent([("", [call("re!member", '{"fact":"unintended"}')]), "done"])
    _ = [event async for event in agent.chat(agent.memory.new_chat(), "go")]
    assert agent.memory.facts() == []
    assert "isn't available" in agent.brain.seen[1][-1]["content"]


@pytest.mark.asyncio
async def test_model_response_closed_on_cancellation():
    started, closed = asyncio.Event(), asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/props"):
            return httpx.Response(200, json={})
        started.set()
        await asyncio.Event().wait()
        return httpx.Response(200, content=b"data: [DONE]\n\n")

    class TrackingTransport(httpx.MockTransport):
        async def handle_async_request(self, request):
            try:
                return await super().handle_async_request(request)
            finally:
                if str(request.url.path).endswith("/chat/completions"):
                    closed.set()

    brain = LlamaCppBrain("http://127.0.0.1:8080", "test", "tok")
    await brain._client.aclose()
    brain._client = httpx.AsyncClient(transport=TrackingTransport(handler), timeout=5.0)
    brain._supports_tool_role = False

    async def consume():
        return [item async for item in brain.stream([{"role": "user", "content": "hi"}], [])]

    task = asyncio.create_task(consume())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await brain.aclose()
    assert closed.is_set()


def test_invalid_server_config_is_rejected_before_resource_creation(make_agent, monkeypatch):
    from dataclasses import replace

    from agent import __main__ as cli

    config = replace(make_agent().config, trusted_proxies=("*",))
    monkeypatch.setattr(cli.Config, "from_env", lambda: config)

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid server settings must not open a DB or model client")

    monkeypatch.setattr(cli, "Memory", forbidden)
    monkeypatch.setattr(cli, "make_brain", forbidden)
    with pytest.raises(SystemExit, match="FORWARDED_ALLOW_IPS"):
        cli.build(validate=True)
