import pytest

from agent.brain import Step, ToolCall
from agent.config import Config
from agent.core import Agent
from agent.memory import Memory
from agent.web.auth import hash_password

HASH = hash_password("correct horse battery staple")


class FakeBrain:
    """Plays back scripted replies. Each item is text, or (text, [ToolCall, ...])."""

    def __init__(self, script):
        self.script = list(script)
        self.seen = []
        self.tools = []

    async def stream(self, messages, tools):
        self.seen.append([dict(m) for m in messages])
        self.tools.append([t["function"]["name"] for t in tools])
        item = self.script.pop(0) if self.script else "ok"
        text, calls = (item, []) if isinstance(item, str) else item
        for word in text.split(" "):
            yield word + " "
        yield Step(text=" ".join(text.split(" ")) + " " if text else "", tool_calls=calls)


def make_config(tmp_path, **kw):
    base = dict(model_base_url="http://x", model_name="fake", model_api_key="",
                password_hash=HASH, cookie_secure=False, session_days=14,
                daily_call_limit=50, max_tool_steps=4, agent_name="Test", timezone="Europe/Helsinki",
                data_dir=tmp_path)
    base.update(kw)
    return Config(**base)


@pytest.fixture
def make_agent(tmp_path):
    def _make(script=(), **kw):
        config = make_config(tmp_path, **kw)
        return Agent(config, Memory(config.db_path), FakeBrain(script))
    return _make


def call(name, args="{}", id="c1"):
    return ToolCall(id=id, name=name, arguments=args)
