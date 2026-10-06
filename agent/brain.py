"""Legacy completions protocol, restricted to the configured local model server."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol

import httpx
from openai import AsyncOpenAI

from .models.endpoint_guard import DEFAULT_MODEL_HOSTS, LocalModelTransport, validate_endpoint


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON text, as the model wrote it

    def args(self) -> dict:
        try:
            value = json.loads(self.arguments or "{}")
            if not isinstance(value, dict):
                raise ValueError("Tool arguments must be a JSON object.")
            return value
        except json.JSONDecodeError as e:
            raise ValueError("Tool arguments must be valid JSON.") from e


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
    def __init__(
        self,
        base_url: str,
        model: str,
        server_token: str,
        *,
        allowed_hosts: tuple[str, ...] = DEFAULT_MODEL_HOSTS,
        timeout: float = 600,
    ):
        validate_endpoint(base_url, allowed_hosts)
        self.model = model
        # M2 replaces this legacy SDK/protocol. It cannot use proxies, redirects or public IPs.
        http_client = httpx.AsyncClient(
            transport=LocalModelTransport(base_url, allowed_hosts),
            trust_env=False,
            follow_redirects=False,
            timeout=timeout,
        )
        completions_url = base_url.rstrip("/")
        if not httpx.URL(completions_url).path.strip("/"):
            completions_url += "/v1"
        self.client = AsyncOpenAI(
            base_url=completions_url,
            api_key=server_token or "local",
            timeout=timeout,
            max_retries=0,
            http_client=http_client,
        )

    async def stream(self, messages: list[dict], tools: list[dict]) -> AsyncIterator[str | Step]:
        kwargs: dict = {"model": self.model, "messages": messages, "stream": True}
        if tools:
            kwargs["tools"] = tools
        response = await self.client.chat.completions.create(**kwargs)
        step = Step()
        calls: dict[int, ToolCall] = {}
        try:
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
        finally:
            await response.close()
        step.tool_calls = [c for _, c in sorted(calls.items()) if c.name]
        for i, call in enumerate(step.tool_calls):
            call.id = call.id or f"call_{i}"
        yield step
