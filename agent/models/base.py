"""Brain protocol shared by local providers and FakeBrain."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol


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
    parse_error: str | None = None
    raw_action: str | None = None
    model_messages: list[dict] | None = None


class Brain(Protocol):
    def stream(self, messages: list[dict], tools: list[dict]) -> AsyncIterator[str | Step]:
        """Yields text pieces (str) while the model types, then one final Step."""
        ...
