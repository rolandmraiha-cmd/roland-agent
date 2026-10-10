"""Local model endpoint validation, DNS pinning and httpx transport coverage."""

import asyncio
import json
import socket

import httpx
import pytest

from agent.brain import Step
from agent.models.endpoint_guard import LocalModelTransport, ModelEndpointRefused, validate_endpoint
from agent.models.llamacpp import LlamaCppBrain


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "::1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "fd00::1",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
    ],
)
def test_local_address_ranges(address):
    host = f"[{address}]" if ":" in address else address
    assert validate_endpoint(f"http://{host}:8080", (address,))


@pytest.mark.parametrize(
    "address",
    [
        "8.8.8.8",
        "0.0.0.0",
        "169.254.169.254",
        "100.64.0.1",
        "192.0.2.1",
        "198.18.0.1",
        "224.0.0.1",
        "240.0.0.1",
        "::",
        "fe80::1",
        "ff02::1",
        "2001:db8::1",
        "2606:4700::1111",
        "::ffff:8.8.8.8",
        "64:ff9b::a00:1",
        "2002:a00:1::",
    ],
)
def test_non_local_ranges_refused_even_when_host_is_allowed(address):
    host = f"[{address}]" if ":" in address else address
    with pytest.raises(ModelEndpointRefused, match="must resolve only"):
        validate_endpoint(f"http://{host}:8080", (address,))


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8080",
        "file:///tmp/model",
        "http://user:password@127.0.0.1",
        "http://127.0.0.1:0",
        "http://127.0.0.1:99999",
        "http://127.0.0.1/?token=test",
        "http://127.0.0.1/#x",
    ],
)
def test_invalid_base_urls_are_refused(url):
    with pytest.raises(ModelEndpointRefused):
        validate_endpoint(url)


def dns_result(address):
    return (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))


@pytest.mark.parametrize("addresses", [[], ["10.0.0.2", "8.8.8.8"], ["8.8.8.8", "10.0.0.2"]])
def test_all_dns_answers_must_be_local(monkeypatch, addresses):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kw: [dns_result(ip) for ip in addresses])
    with pytest.raises(ModelEndpointRefused):
        validate_endpoint("http://model:8080")


async def test_dns_is_checked_each_request_and_pinned(monkeypatch):
    answers = iter(["10.0.0.2", "8.8.8.8"])
    seen = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kw: [dns_result(next(answers))])

    async def handler(request):
        seen.append(request)
        return httpx.Response(200, text="ok")

    transport = LocalModelTransport("http://model:8080")
    await transport.inner.aclose()
    transport.inner = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        assert (await client.get("http://model:8080/v1/models")).status_code == 200
        with pytest.raises(ModelEndpointRefused, match="must resolve only"):
            await client.get("http://model:8080/v1/models")
    assert len(seen) == 1
    assert seen[0].url.host == "10.0.0.2"
    assert seen[0].headers["Host"] == "model:8080"


async def test_redirects_are_refused_even_when_caller_enables_them():
    seen = []

    async def handler(request):
        seen.append(request)
        return httpx.Response(307, headers={"Location": "http://127.0.0.1:8080/other"})

    transport = LocalModelTransport("http://127.0.0.1:8080")
    await transport.inner.aclose()
    transport.inner = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        with pytest.raises(ModelEndpointRefused, match="redirects are refused"):
            await client.get("http://127.0.0.1:8080/v1/models")
    assert len(seen) == 1


async def test_changed_origin_is_refused_before_request():
    transport = LocalModelTransport("http://127.0.0.1:8080")
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ModelEndpointRefused, match="configured local origin"):
            await client.get("http://127.0.0.1:8081/v1/models")


async def test_sdk_stream_uses_local_guard_and_ignores_proxies(monkeypatch):
    """Use a loopback HTTP fixture to verify the real SDK uses the supplied client."""
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    received = []

    async def serve(reader, writer):
        header = await reader.readuntil(b"\r\n\r\n")
        received.append(header)
        length = next(
            int(line.split(b":", 1)[1])
            for line in header.split(b"\r\n")
            if line.lower().startswith(b"content-length:")
        )
        await reader.readexactly(length)
        chunk = {
            "id": "fixture",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "test",
            "choices": [{"index": 0, "delta": {"content": "hello"}, "finish_reason": None}],
        }
        body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    brain = LlamaCppBrain(f"http://127.0.0.1:{port}", "test", "local-test-token", tool_mode="native")
    brain._supports_tool_role = False
    try:
        result = [event async for event in brain.stream([{"role": "user", "content": "test"}], [])]
    finally:
        await brain.aclose()
        server.close()
        await server.wait_closed()
    assert result[0] == "hello"
    assert isinstance(result[-1], Step) and result[-1].text == "hello"
    assert len(received) == 1
    assert received[0].startswith(b"POST /v1/chat/completions HTTP/1.1")
    assert b"authorization: bearer local-test-token" in received[0].lower()
