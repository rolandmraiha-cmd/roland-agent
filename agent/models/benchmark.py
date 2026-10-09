"""Measure a fixed public prompt on the configured local llama.cpp server."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import time

import httpx

from ..config import Config
from .endpoint_guard import LocalModelTransport, validate_endpoint
from .llamacpp import _roots

PROMPT = (
    "List ten practical ways to organise a small workspace. Explain each in one sentence. "
    "Use plain English. Include storage, lighting, cables, tools, cleaning and planning. "
    "Do not call tools. Start directly with the list."
)


def rates(payload: dict) -> dict:
    timing = payload.get("timings")
    if not isinstance(timing, dict):
        raise ValueError("model response omitted timings")
    result = {}
    for key in ("prompt_n", "predicted_n", "prompt_per_second", "predicted_per_second"):
        value = timing.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError("model response has invalid timings")
        result[key] = value
    return result


async def sample(client: httpx.AsyncClient, root: str, *, clock=time.monotonic) -> dict:
    start = clock()
    first = None
    final = None
    async with client.stream(
        "POST",
        f"{root}/completion",
        json={
            "prompt": PROMPT,
            "n_predict": 128,
            "temperature": 0,
            "seed": 42,
            "stream": True,
            "cache_prompt": False,
        },
    ) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            payload = json.loads(data)
            if not isinstance(payload, dict) or payload.get("error"):
                raise ValueError("model benchmark stream failed")
            if payload.get("content") and first is None:
                first = clock() - start
            if payload.get("stop") is True:
                final = rates(payload)
    if first is None or final is None:
        raise ValueError("model benchmark stream ended without text or timings")
    return {"time_to_first_token_s": round(first, 3), "elapsed_s": round(clock() - start, 3), **final}


async def benchmark(config: Config, count: int) -> dict:
    if config.model_provider != "llamacpp":
        raise ValueError("this benchmark requires the local llama.cpp provider")
    validate_endpoint(config.model_base_url, config.model_allowed_hosts)
    root, _ = _roots(config.model_base_url)
    async with httpx.AsyncClient(
        transport=LocalModelTransport(config.model_base_url, config.model_allowed_hosts),
        trust_env=False,
        follow_redirects=False,
        timeout=config.model_timeout_s,
        headers={"Authorization": f"Bearer {config.model_server_token}"} if config.model_server_token else {},
    ) as client:
        samples = [await sample(client, root) for _ in range(count)]
    return {
        "model": config.model_name,
        "configured_context": config.model_ctx,
        "configured_threads": config.model_threads,
        "prompt_cache": False,
        "samples": samples,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, choices=range(1, 6), default=3)
    args = parser.parse_args()
    from ..__main__ import harden_process

    harden_process()
    try:
        result = asyncio.run(benchmark(Config.from_env(), args.samples))
    except (ValueError, httpx.HTTPError):
        raise SystemExit("Benchmark failed; check local model health and server timing support") from None
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
