"""Select the local model provider from config."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .endpoint_guard import ModelEndpointRefused, validate_endpoint
from .llamacpp import LlamaCppBrain
from .ollama import OllamaBrain

if TYPE_CHECKING:
    from agent.config import Config

    from .base import Brain


def make_brain(config: Config) -> Brain:
    """Build LlamaCppBrain or OllamaBrain; refuse non-local endpoints."""
    try:
        validate_endpoint(config.model_base_url, config.model_allowed_hosts)
    except ModelEndpointRefused as error:
        raise SystemExit(str(error)) from error
    if config.model_provider == "llamacpp":
        return LlamaCppBrain(
            config.model_base_url,
            config.model_name,
            config.model_server_token,
            allowed_hosts=config.model_allowed_hosts,
            temperature=config.model_temperature,
            max_new_tokens=config.model_max_new_tokens,
            timeout=config.model_timeout_s,
            tool_mode=config.model_tool_mode,
            ctx=config.model_ctx,
        )
    if config.model_provider == "ollama":
        return OllamaBrain(
            config.model_base_url,
            config.model_name,
            config.model_server_token,
            allowed_hosts=config.model_allowed_hosts,
            temperature=config.model_temperature,
            max_new_tokens=config.model_max_new_tokens,
            timeout=config.model_timeout_s,
            tool_mode=config.model_tool_mode,
            ctx=config.model_ctx,
        )
    raise SystemExit("MODEL_PROVIDER must be llamacpp or ollama")
