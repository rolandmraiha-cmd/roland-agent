"""Authentication and request controls for both HTTP and websocket connections."""

from __future__ import annotations

import hmac
import ipaddress
import logging
from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..config import Config, core_peer_networks, trusted_proxy_networks
from .auth import Sessions, cookie_name, csrf_token

DIRECT_PEER = "agent.direct_peer"
PUBLIC_PATHS = {"/login", "/favicon.ico", "/healthz"}
log = logging.getLogger("agent.web")


def _address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        address = ipaddress.ip_address(value)
        if isinstance(address, ipaddress.IPv6Address):
            return address.ipv4_mapped or address
        return address
    except ValueError:
        return None


def _matches(value: str, networks: list) -> bool:
    address = _address(value)
    return address is not None and any(address in network for network in networks)


def _header(headers: Headers, name: str) -> str:
    values = headers.getlist(name)
    return values[0] if len(values) == 1 else ""


def internal_path(path: str) -> bool:
    return path == "/internal" or path.startswith("/internal/")


async def _deny(scope: Scope, receive: Receive, send: Send, status: int, message: str) -> None:
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 4401 if status == 401 else 4403})
    else:
        await JSONResponse({"error": message}, status_code=status)(scope, receive, send)


class PeerAllowlist:
    def __init__(self, app: ASGIApp, config: Config):
        self.app = app
        self.enabled = bool(config.core_allowed_peers)
        self.networks = core_peer_networks(config.core_allowed_peers + ("127.0.0.1", "::1"))
        self.bind_address = _address(config.host)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in {"http", "websocket"}:
            peer = scope.get("client")
            host = peer[0] if peer else ""
            # This address is saved before any trusted proxy changes scope['client'].
            scope = dict(scope)
            scope[DIRECT_PEER] = peer
            own_healthcheck = (
                scope["type"] == "http"
                and scope.get("method") in {"GET", "HEAD"}
                and scope["path"] == "/healthz"
                and self.bind_address is not None
                and not self.bind_address.is_unspecified
                and _address(host) == self.bind_address
            )
            if self.enabled and not (_matches(host, self.networks) or own_healthcheck):
                await _deny(scope, receive, send, 403, "peer not allowed")
                return
        await self.app(scope, receive, send)


def _strip_port(hop: str) -> str:
    """'1.2.3.4:5678' becomes '1.2.3.4'; '[2001:db8::1]:443' becomes '2001:db8::1'."""
    hop = hop.strip()
    if hop.startswith("["):
        return hop[1 : hop.find("]")] if "]" in hop else hop
    if hop.count(":") == 1:
        return hop.split(":")[0]
    return hop


class ProxyHeaders:
    """Trust forwarded visitor addresses only from explicitly configured proxies."""

    def __init__(self, app: ASGIApp, trusted: tuple[str, ...]):
        self.app = app
        self.nets = trusted_proxy_networks(trusted)
        self.warned = False
        self.warned_all_trusted = False

    def trusted(self, host: str) -> bool:
        return _matches(host, self.nets)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in {"http", "websocket"} and scope.get("client"):
            host, port = scope["client"]
            scope = dict(scope)
            scope.setdefault(DIRECT_PEER, scope["client"])
            fwd = ",".join(v.decode("latin-1") for k, v in scope["headers"] if k == b"x-forwarded-for")
            if fwd and self.trusted(host):
                hops = [_strip_port(h) for h in fwd.split(",") if h.strip()]
                visitor = next((h for h in reversed(hops) if not self.trusted(h)), None)
                if visitor is not None:
                    host = visitor
                elif hops and not self.warned_all_trusted:
                    self.warned_all_trusted = True
                    log.warning("Every X-Forwarded-For hop is trusted; keeping direct peer %s.", host)
                proto = _header(Headers(scope=scope), "x-forwarded-proto")
                scope["client"] = (host, port)
                if proto in {"http", "https"}:
                    scope["scheme"] = (
                        proto if scope["type"] == "http" else ("wss" if proto == "https" else "ws")
                    )
            elif fwd and not self.warned:
                self.warned = True
                log.warning(
                    "Got X-Forwarded-For from %s, which isn't in FORWARDED_ALLOW_IPS, so "
                    "it was ignored. If that's your reverse proxy, add its IP there; "
                    "otherwise every visitor shares one login lockout.",
                    host,
                )
        await self.app(scope, receive, send)


class AuthMiddleware:
    def __init__(self, app: ASGIApp, config: Config, sessions: Sessions):
        self.app, self.config, self.sessions = app, config, sessions
        self.cookie = cookie_name(config)
        self.internal_peers = core_peer_networks(config.core_allowed_peers)
        self.proxies = trusted_proxy_networks(config.trusted_proxies)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        if internal_path(path):
            peer = scope.get(DIRECT_PEER)
            host = peer[0] if peer else ""
            if scope["type"] == "websocket" or not (
                _matches(host, self.internal_peers) and _matches(host, self.proxies)
            ):
                await _deny(scope, receive, send, 403, "internal peer not allowed")
                return
            await self.app(scope, receive, send)
            return
        public = scope["type"] == "http" and (path in PUBLIC_PATHS or path.startswith("/static/"))
        if not public and not self.sessions.valid(HTTPConnection(scope).cookies.get(self.cookie)):
            if scope["type"] == "http" and not path.startswith("/api/"):
                await RedirectResponse("/login", status_code=303)(scope, receive, send)
            else:
                await _deny(scope, receive, send, 401, "not logged in")
            return
        if scope["type"] == "websocket":
            headers = Headers(scope=scope)
            host = self.config.agent_host or _header(headers, "host")
            origin = _header(headers, "origin")
            schemes = ("https",) if self.config.cookie_secure else ("http", "https")
            if not host or origin not in {f"{scheme}://{host}" for scheme in schemes}:
                await _deny(scope, receive, send, 403, "bad origin")
                return
        await self.app(scope, receive, send)


class CSRFMiddleware:
    def __init__(self, app: ASGIApp, config: Config):
        self.app, self.config = app, config
        self.cookie = cookie_name(config)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and (scope["method"] not in {"GET", "HEAD", "OPTIONS"} or scope["path"] == "/api/training/export")
            and not internal_path(scope["path"])
        ):
            headers = Headers(scope=scope)
            # An explicit malformed/null/duplicate Origin must not fall back to Referer.
            raw = _header(headers, "origin") if "origin" in headers else _header(headers, "referer")
            try:
                origin = urlsplit(raw)
                schemes = {"https"} if self.config.cookie_secure else {"http", "https"}
                valid = origin.scheme in schemes and origin.netloc == _header(headers, "host")
            except ValueError:
                valid = False
            if not valid:
                await _deny(scope, receive, send, 403, "bad origin")
                return
            token = HTTPConnection(scope).cookies.get(self.cookie)
            if scope["path"] != "/login" and token:
                sent = _header(headers, "x-csrf-token")
                if not hmac.compare_digest(sent.encode("utf-8"), csrf_token(token).encode("ascii")):
                    await _deny(scope, receive, send, 403, "bad csrf token")
                    return
        await self.app(scope, receive, send)


def security_headers(config: Config) -> dict[str, str]:
    connect = "'self'" + (f" wss://{config.agent_host}" if config.agent_host else "")
    return {
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
            f"connect-src {connect}; frame-src 'self'; object-src 'none'; base-uri 'none'; "
            "form-action 'self'; frame-ancestors 'none'"
        ),
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "same-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), clipboard-read=(), usb=(), payment=()",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-origin",
    }


class SecurityHeaders:
    """Wrap all HTTP responses, including denials from the access-control middleware."""

    def __init__(self, app: ASGIApp, config: Config):
        self.app = app
        self.headers = security_headers(config)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def secure_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = dict(message, headers=list(message.get("headers", ())))
                headers = MutableHeaders(scope=message)
                path = scope["path"]
                for name, value in self.headers.items():
                    # File download/preview use a stricter CSP (§6.4); keep the route's value.
                    if name == "Content-Security-Policy" and path in {
                        "/api/files/download",
                        "/api/files/preview",
                    }:
                        continue
                    headers[name] = value
                if path.startswith("/api/") or internal_path(path) or path in {"/", "/login", "/healthz", "/screen"}:
                    headers["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, secure_send)
