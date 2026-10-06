"""Local provider, action parse, context budget and factory coverage (A2.6)."""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from agent.brain import Step, make_brain
from agent.config import Config
from agent.core import Agent
from agent.memory import Memory
from agent.models.action import assert_schema_subset, build_action_schema
from agent.models.context import estimate_tokens, fit_messages
from agent.models.endpoint_guard import ModelEndpointRefused, validate_endpoint
from agent.models.llamacpp import LlamaCppBrain
from agent.models.ollama import OllamaBrain
from agent.models.parse import parse_action, repair_action
from agent.tools import TOOLS, schemas
from tests.conftest import HASH, make_config


def test_endpoint_guard_refuses_public_and_https_hosts(monkeypatch):
    with pytest.raises(ModelEndpointRefused):
        validate_endpoint("https://api.example.com")
    with pytest.raises(ModelEndpointRefused):
        validate_endpoint("http://8.8.8.8:8080", ("8.8.8.8",))
    monkeypatch.setattr(
        "agent.models.endpoint_guard.socket.getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("8.8.8.8", 0))],
    )
    with pytest.raises(ModelEndpointRefused):
        validate_endpoint("http://model:8080")


def test_endpoint_guard_accepts_internal():
    assert validate_endpoint("http://127.0.0.1:8080")
    assert validate_endpoint("http://10.77.6.60:8080")


def test_action_schema_subset():
    for name, (schema, _) in TOOLS.items():
        assert_schema_subset(schema["function"]["parameters"], name)


def test_parse_repairs_common_slips():
    schema = build_action_schema(schemas())
    offered = set(TOOLS)
    cases = [
        '{"name":"remember","arguments":{"fact":"hi"}}',
        '{"action":"tool","tool":"remember","args":"{\\"fact\\":\\"hi\\"}"}',
        '{"action":"tool_call","tool":"remember","args":{"fact":"hi"}}',
    ]
    for raw in cases:
        step = parse_action(raw, schema, offered)
        assert step.parse_error is None
        assert step.tool_calls and step.tool_calls[0].name == "remember"
    assert repair_action({"action": "reply", "text": "ok"})["action"] == "reply"


def test_parse_rejects_unknown_tool():
    schema = build_action_schema(schemas())
    step = parse_action('{"action":"tool","tool":"not_a_tool","args":{}}', schema, set(TOOLS))
    assert step.parse_error and "tool" in step.parse_error


@pytest.mark.asyncio
async def test_streaming_reply_text_is_extracted_incrementally():
    chunks = [
        '{"action":"reply","text":"he',
        'llo \\"w',
        'orld\\" \\u00e9"}',
    ]
    from agent.models.parse import TextFieldStreamer

    streamer = TextFieldStreamer()
    out = []
    for chunk in chunks:
        out.extend(streamer.feed(chunk))
    assert "".join(out) == 'hello "world" é'


@pytest.mark.asyncio
async def test_llamacpp_request_has_json_schema_and_bearer():
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/props"):
            return httpx.Response(200, json={})
        body = (
            'data: {"choices":[{"delta":{"content":"{\\"action\\":\\"reply\\",'
            '\\"text\\":\\"hi\\"}"},"finish_reason":null}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

    brain = LlamaCppBrain("http://127.0.0.1:8080", "current", "secret-token")
    await brain._client.aclose()
    transport = httpx.MockTransport(handler)
    brain._client = httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:8080",
        headers={"Authorization": "Bearer secret-token"},
    )
    items = [item async for item in brain.stream([{"role": "user", "content": "hi"}], [])]
    await brain.aclose()
    assert any(isinstance(item, Step) and item.text == "hi" for item in items)
    chat_reqs = [r for r in seen if r.url.path.endswith("/chat/completions")]
    assert chat_reqs
    assert chat_reqs[0].headers.get("Authorization") == "Bearer secret-token"
    payload = json.loads(chat_reqs[0].content)
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["cache_prompt"] is True


@pytest.mark.asyncio
async def test_ollama_request_has_format_schema():
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = (
            json.dumps(
                {
                    "message": {"content": '{"action":"reply","text":"yo"}'},
                    "done": True,
                }
            )
            + "\n"
        )
        return httpx.Response(200, content=body.encode())

    brain = OllamaBrain("http://127.0.0.1:11434", "current")
    await brain._client.aclose()
    brain._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:11434"
    )
    items = [item async for item in brain.stream([{"role": "user", "content": "hi"}], schemas()[:1])]
    await brain.aclose()
    assert isinstance(items[-1], Step) and items[-1].text == "yo"
    payload = json.loads(seen[0].content)
    assert isinstance(payload["format"], dict)
    assert "oneOf" in payload["format"]


@pytest.mark.asyncio
async def test_parse_error_triggers_retry_then_gives_up(tmp_path):
    class BadBrain:
        def __init__(self):
            self.calls = 0

        async def stream(self, messages, tools):
            self.calls += 1
            yield Step(text="not-json", parse_error="no JSON object found")

    config = make_config(tmp_path, model_parse_retries=2, daily_call_limit=20)
    brain = BadBrain()
    agent = Agent(config, Memory(config.db_path), brain)
    events = [event async for event in agent.chat(agent.memory.new_chat(), "hi")]
    assert brain.calls == 3  # initial + 2 retries
    assert events[-1]["type"] == "error"
    assert "after 2 tries" in events[-1]["message"]
    assert not any(event["type"] == "tool" for event in events)


@pytest.mark.asyncio
async def test_context_budget_trims_oldest_first_and_keeps_system_and_last_user():
    messages = [{"role": "system", "content": "sys"}]
    for i in range(20):
        messages.append({"role": "user", "content": f"old-{i} " + ("x" * 300)})
        messages.append({"role": "assistant", "content": f"ans-{i} " + ("y" * 300)})
    messages.append({"role": "user", "content": "LATEST"})
    fitted = await fit_messages(
        messages,
        budget=800,
        history_limit=12,
        tool_output_chars=3000,
        counter=None,
    )
    assert fitted[0]["role"] == "system" and fitted[0]["content"] == "sys"
    assert fitted[-1]["content"] == "LATEST"
    assert all(m.get("content") != "old-0 " + ("x" * 300) for m in fitted)


def test_model_api_key_is_ignored_with_warning(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("MODEL_API_KEY", "should-be-ignored")
    monkeypatch.setenv("MODEL_SERVER_TOKEN", "local-token")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_PASSWORD_HASH", HASH)
    monkeypatch.setenv("MODEL_BASE_URL", "http://127.0.0.1:8080")
    with caplog.at_level(logging.WARNING):
        config = Config.from_env()
    assert "MODEL_API_KEY is removed" in caplog.text
    assert not hasattr(config, "model_api_key")


def test_factory_selects_provider(tmp_path):
    llama = make_brain(make_config(tmp_path, model_provider="llamacpp"))
    assert isinstance(llama, LlamaCppBrain)
    ollama = make_brain(
        make_config(tmp_path, model_provider="ollama", model_base_url="http://127.0.0.1:11434")
    )
    assert isinstance(ollama, OllamaBrain)


def test_estimate_tokens_fallback():
    assert estimate_tokens("abcdef") == 2
