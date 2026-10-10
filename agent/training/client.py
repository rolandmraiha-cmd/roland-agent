"""Core talks only to the private trainer control endpoint, without proxy discovery."""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx


class TrainerClient:
    def __init__(self, config):
        parsed = urlsplit(config.trainer_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "10.77.7.70"
            or parsed.port != 7200
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
        ):
            raise ValueError("TRAINER_URL must be the private trainer control address")
        self.url, self.token = config.trainer_url.rstrip("/"), config.trainer_api_token

    async def request(self, method: str, path: str, *, body=None, content=None):
        if not path.startswith("/v1/"):
            raise ValueError("Invalid trainer endpoint")
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=600) as client:
            response = await client.request(
                method,
                self.url + path,
                json=body,
                content=content,
                headers={"Authorization": "Bearer " + self.token},
            )
            response.raise_for_status()
            return response.json()
