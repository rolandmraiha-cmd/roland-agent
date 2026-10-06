"""Refuse public model destinations and pin checked addresses at request time."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import SplitResult, urlsplit

import httpx

DEFAULT_MODEL_HOSTS = ("10.77.6.60", "model", "127.0.0.1", "localhost", "::1")
PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "fc00::/7",
    )
)


class ModelEndpointRefused(ValueError):
    """The model destination does not satisfy the local-only policy."""


def parse_endpoint(base_url: str, allowed_hosts: tuple[str, ...]) -> SplitResult:
    try:
        parsed = urlsplit(base_url)
        host = parsed.hostname
        port = parsed.port
        if (
            parsed.scheme != "http"
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise ValueError("invalid model URL")
    except ValueError as error:
        raise ModelEndpointRefused(
            "MODEL_BASE_URL must be an http URL without credentials, query or fragment"
        ) from error
    if host.lower() not in {entry.lower() for entry in allowed_hosts}:
        raise ModelEndpointRefused("MODEL_BASE_URL host must be in MODEL_ALLOWED_HOSTS")
    return parsed


def resolve_local(host: str) -> str:
    try:
        try:
            addresses = [str(ipaddress.ip_address(host))]
        except ValueError:
            addresses = [item[4][0] for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)]
        if not addresses:
            raise ValueError("no addresses")
        checked = []
        for address in addresses:
            if "%" in address:
                raise ValueError("scoped address")
            ip = ipaddress.ip_address(address)
            if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
                ip = ip.ipv4_mapped
            if not (ip.is_loopback or any(ip in network for network in PRIVATE_NETWORKS)):
                raise ValueError("non-local address")
            checked.append(str(ip))
        return checked[0]
    except (OSError, ValueError, UnicodeError) as error:
        raise ModelEndpointRefused(
            "MODEL_BASE_URL must resolve only to loopback, RFC1918 or ULA addresses"
        ) from error


def validate_endpoint(base_url: str, allowed_hosts: tuple[str, ...] = DEFAULT_MODEL_HOSTS) -> str:
    parsed = parse_endpoint(base_url, allowed_hosts)
    return resolve_local(parsed.hostname)


class LocalModelTransport(httpx.AsyncBaseTransport):
    """Check every request, retain Host, connect to a literal IP and reject redirects."""

    def __init__(self, base_url: str, allowed_hosts: tuple[str, ...] = DEFAULT_MODEL_HOSTS):
        self.endpoint = parse_endpoint(base_url, allowed_hosts)
        self.inner = httpx.AsyncHTTPTransport(trust_env=False, retries=0)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if (
            request.url.scheme != "http"
            or request.url.host != self.endpoint.hostname
            or (request.url.port or 80) != (self.endpoint.port or 80)
        ):
            raise ModelEndpointRefused("Model requests must stay on the configured local origin")
        ip = await asyncio.to_thread(resolve_local, self.endpoint.hostname)
        headers = request.headers.copy()
        headers["Host"] = self.endpoint.netloc
        pinned = httpx.Request(
            request.method,
            request.url.copy_with(host=ip),
            headers=headers,
            stream=request.stream,
            extensions=request.extensions,
        )
        response = await self.inner.handle_async_request(pinned)
        if 300 <= response.status_code < 400:
            await response.aclose()
            raise ModelEndpointRefused("Model redirects are refused")
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()
