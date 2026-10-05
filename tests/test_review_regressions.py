import asyncio
import json
from types import SimpleNamespace

import pytest

from agent.brain import OpenAICompatibleBrain, ToolCall
from agent.tools import MAX_OUTPUT, ToolContext, call_tool
from conftest import call


@pytest.mark.asyncio
@pytest.mark.parametrize('limit', [0, 1, 2])
async def test_tool_budget_never_runs_an_extra_round(make_agent, limit):
    script = [('', [call('remember', json.dumps({'fact': f'round {i}'}))])
              for i in range(limit + 1)]
    agent = make_agent(script, max_tool_steps=limit)
    events = [event async for event in agent.chat(agent.memory.new_chat(), 'go')]
    assert len(agent.memory.facts()) == limit
    assert events[-1]['type'] == 'error'
    assert len(agent.brain.seen) == limit + 1


@pytest.mark.asyncio
@pytest.mark.parametrize('arguments', ['{"content":"oops",', '[]', 'null', '42'])
async def test_invalid_arguments_do_not_write_files(make_agent, arguments):
    agent = make_agent([('', [call('write_file', arguments)]), 'corrected'])
    events = [event async for event in agent.chat(agent.memory.new_chat(), 'go')]
    assert not list(agent.config.workspace.iterdir())
    assert 'Error: Tool arguments' in agent.brain.seen[1][-1]['content']
    assert not any(event['type'] == 'tool' for event in events)


def test_valid_empty_arguments_remain_supported():
    assert ToolCall('c', 'list_files', '').args() == {}


@pytest.mark.asyncio
async def test_large_file_read_is_bounded(make_agent, monkeypatch):
    agent = make_agent()
    path = agent.config.workspace / 'large.txt'
    path.write_text('x' * (MAX_OUTPUT * 20))
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

    monkeypatch.setattr(Path, 'open', lambda self, *a, **kw: CheckedReader(original(self, *a, **kw)))
    result = await call_tool(agent.ctx, 'read_file', {'path': 'large.txt'})
    assert reads == [MAX_OUTPUT + 1]
    assert result.startswith('x' * MAX_OUTPUT) and 'file continues' in result


@pytest.mark.asyncio
async def test_shell_cancellation_kills_process_group(make_agent, monkeypatch):
    from agent import tools
    started = asyncio.Event()
    killed = []

    async def read(size):
        started.set()
        await asyncio.Event().wait()

    async def communicate():
        return b'', b''

    proc = SimpleNamespace(pid=12345, stdout=SimpleNamespace(read=read), communicate=communicate)

    async def spawn(*args, **kwargs):
        assert kwargs['start_new_session'] is True
        return proc

    monkeypatch.setattr(tools.asyncio, 'create_subprocess_shell', spawn)
    monkeypatch.setattr(tools.os, 'killpg', lambda pid, sig: killed.append((pid, sig)))
    agent = make_agent()
    ctx = ToolContext(agent.memory, agent.config.workspace, agent.config.timezone, True)
    task = asyncio.create_task(call_tool(ctx, 'run_shell', {'command': 'sleep 60'}))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert killed == [(12345, 9)]


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [False, True])
async def test_model_response_closed_on_completion_or_error(failure):
    closed = []

    class Response:
        def __aiter__(self):
            return self.chunks()
        async def chunks(self):
            if failure:
                raise RuntimeError('stream failed')
            yield SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content='hello', tool_calls=None))])
        async def close(self):
            closed.append(True)

    async def create(**kwargs):
        return Response()

    brain = OpenAICompatibleBrain.__new__(OpenAICompatibleBrain)
    brain.model = 'fake'
    brain.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    if failure:
        with pytest.raises(RuntimeError):
            _ = [item async for item in brain.stream([], [])]
    else:
        items = [item async for item in brain.stream([], [])]
        assert items[-1].text == 'hello'
    assert closed == [True]


@pytest.mark.asyncio
async def test_modified_tool_name_cannot_dispatch_an_action(make_agent):
    agent = make_agent([('', [call('re!member', '{"fact":"unintended"}')]), 'done'])
    _ = [event async for event in agent.chat(agent.memory.new_chat(), 'go')]
    assert agent.memory.facts() == []
    assert "isn't available" in agent.brain.seen[1][-1]['content']


@pytest.mark.asyncio
async def test_model_response_closed_on_cancellation():
    started, closed = asyncio.Event(), asyncio.Event()

    class Response:
        def __aiter__(self):
            return self.chunks()
        async def chunks(self):
            started.set()
            await asyncio.Event().wait()
            yield None
        async def close(self):
            closed.set()

    async def create(**kwargs):
        return Response()

    brain = OpenAICompatibleBrain.__new__(OpenAICompatibleBrain)
    brain.model = 'fake'
    brain.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    async def consume():
        return [item async for item in brain.stream([], [])]

    task = asyncio.create_task(consume())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
