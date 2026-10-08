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
from agent.models.parse import CUT_OFF_NOTE, parse_action, repair_action
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


def llamacpp_with(chunks: list[str], finish_reason: str | None, seen: list | None = None) -> LlamaCppBrain:
    """A llama.cpp brain whose server streams `chunks` and then stops for `finish_reason`."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request.url.path)
        if request.url.path.endswith("/props"):
            return httpx.Response(200, json={})
        if not request.url.path.endswith("/chat/completions"):
            return httpx.Response(404)
        events = [{"choices": [{"delta": {"content": chunk}, "finish_reason": None}]} for chunk in chunks]
        events.append({"choices": [{"delta": {}, "finish_reason": finish_reason}]})
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

    brain = LlamaCppBrain("http://127.0.0.1:8080", "current", "secret-token")
    brain._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8080")
    return brain


async def final_step(brain, tools=()) -> Step:
    items = [item async for item in brain.stream([{"role": "user", "content": "hi"}], list(tools))]
    await brain.aclose()
    assert isinstance(items[-1], Step)
    return items[-1]


@pytest.mark.asyncio
async def test_a_reply_cut_off_at_the_length_limit_is_kept_with_a_note():
    brain = llamacpp_with(['{"action": "reply", "text": "Part one ', 'of a long \\"answer\\" and'], "length")
    step = await final_step(brain)
    assert step.parse_error is None and not step.tool_calls
    assert step.text == 'Part one of a long "answer" and' + CUT_OFF_NOTE


@pytest.mark.asyncio
async def test_a_tool_call_cut_off_at_the_length_limit_is_still_an_error():
    brain = llamacpp_with(['{"action": "tool", "tool": "remember", "args": {"fact": "hal'], "length")
    step = await final_step(brain, schemas())
    assert step.parse_error == "output truncated at max_tokens"
    assert not step.tool_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_reason", ["stop", "length"])
async def test_a_reply_written_inside_a_reply_shows_only_the_text(finish_reason):
    """Seen on Contabo: the model put a whole reply action, escaped, into its reply text."""
    inner = '{\n  "action": "reply",\n  "text": "A car engine.\\n\\n1. Intake'
    if finish_reason == "stop":
        inner += '"\n}'
    outer = '{"action": "reply", "text": ' + json.dumps(inner)
    if finish_reason == "stop":
        outer += "}"
    else:
        outer = outer[:-1]  # cut off before the closing quote
    step = await final_step(llamacpp_with([outer], finish_reason))
    assert step.parse_error is None
    expected = "A car engine.\n\n1. Intake"
    assert step.text == (expected + CUT_OFF_NOTE if finish_reason == "length" else expected)


def test_streamer_joins_escaped_surrogate_pairs_even_across_chunks():
    """An emoji in JSON is two \\u escapes; each half alone can't be sent as UTF-8."""
    from agent.models.parse import TextFieldStreamer

    streamer = TextFieldStreamer()
    out = []
    for chunk in ['{"action":"reply","text":"ok \\ud83d', "\\ude00 \\ud83d x \\ude00", ' end"}']:
        out.extend(streamer.feed(chunk))
    assert "".join(out) == streamer.emitted == "ok \U0001f600 � x � end"
    streamer.emitted.encode("utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_reason", ["stop", "length"])
async def test_an_emoji_in_a_nested_reply_survives(finish_reason):
    inner = '{"action": "reply", "text": "smile \\ud83d\\ude00 and a lone \\ud83d'
    if finish_reason == "stop":
        inner += '"}'
    outer = '{"action": "reply", "text": ' + json.dumps(inner)
    outer = outer + "}" if finish_reason == "stop" else outer[:-1]
    step = await final_step(llamacpp_with([outer], finish_reason))
    assert step.parse_error is None
    assert step.text.startswith("smile \U0001f600 and a lone")
    step.text.encode("utf-8")  # no half pairs left


@pytest.mark.asyncio
async def test_ollama_keeps_a_cut_off_reply_too():
    async def handler(request: httpx.Request) -> httpx.Response:
        lines = [
            {"message": {"content": '{"action":"reply","text":"Half an ans'}, "done": False},
            {"message": {"content": "wer"}, "done": True, "done_reason": "length"},
        ]
        return httpx.Response(200, content="".join(json.dumps(line) + "\n" for line in lines).encode())

    brain = OllamaBrain("http://127.0.0.1:11434", "current")
    await brain._client.aclose()
    brain._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:11434")
    step = await final_step(brain, schemas()[:1])
    assert step.parse_error is None
    assert step.text == "Half an answer" + CUT_OFF_NOTE


@pytest.mark.asyncio
async def test_the_agent_does_not_ask_again_for_a_cut_off_reply(tmp_path):
    seen: list[str] = []
    brain = llamacpp_with(['{"action": "reply", "text": "A long answer that stops'], "length", seen)
    config = make_config(tmp_path, model_parse_retries=2, daily_call_limit=20)
    agent = Agent(config, Memory(config.db_path), brain)
    events = [event async for event in agent.chat(agent.memory.new_chat(), "Explain engines in detail")]
    await brain.aclose()
    assert seen.count("/v1/chat/completions") == 1
    assert events[-1]["type"] == "done"
    assert events[-1]["reply"] == "A long answer that stops" + CUT_OFF_NOTE


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
