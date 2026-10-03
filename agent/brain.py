"""The agent's brain: any OpenAI-compatible chat API (Grok, OpenAI, Ollama, vLLM...)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol

from openai import AsyncOpenAI


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON text, as the model wrote it

    def args(self) -> dict:
        try:
            value = json.loads(self.arguments or "{}")
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}


@dataclass
class Step:
    """One streamed model reply: text pieces as they arrive, then any tool calls at the end."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


class Brain(Protocol):
    def stream(self, messages: list[dict], tools: list[dict]) -> AsyncIterator[str | Step]:
        """Yields text pieces (str) while the model types, then one final Step."""
        ...


class OpenAICompatibleBrain:
    def __init__(self, base_url: str, model: str, api_key: str):
        self.model = model
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key or "none", timeout=300)

    async def stream(self, messages: list[dict], tools: list[dict]) -> AsyncIterator[str | Step]:
        kwargs: dict = {"model": self.model, "messages": messages, "stream": True}
        if tools:
            kwargs["tools"] = tools
        response = await self.client.chat.completions.create(**kwargs)
        step = Step()
        calls: dict[int, ToolCall] = {}
        async for chunk in response:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                step.text += delta.content
                yield delta.content
            for tc in delta.tool_calls or []:
                call = calls.setdefault(tc.index, ToolCall(id="", name="", arguments=""))
                if tc.id:
                    call.id = tc.id
                if tc.function and tc.function.name:
                    call.name += tc.function.name
                if tc.function and tc.function.arguments:
                    call.arguments += tc.function.arguments
        step.tool_calls = [c for _, c in sorted(calls.items()) if c.name]
        for i, call in enumerate(step.tool_calls):
            call.id = call.id or f"call_{i}"
        yield step
