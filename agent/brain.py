"""Compatibility re-exports for the local model Brain protocol."""

from __future__ import annotations

from .models.base import Brain, Step, ToolCall
from .models.factory import make_brain

__all__ = ["Brain", "Step", "ToolCall", "make_brain"]
