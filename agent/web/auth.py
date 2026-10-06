"""Login for the web page: one password, server-side sessions, and a lockout on wrong guesses."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import secrets
import time
from collections import defaultdict, deque
from typing import TYPE_CHECKING

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from ..memory import Memory

if TYPE_CHECKING:
    from ..config import Config

_hasher = PasswordHasher()  # argon2id with the library's recommended settings

COOKIE = "agent_session"


def cookie_name(config: Config) -> str:
    return "__Host-agent_session" if config.cookie_secure else COOKIE


PER_IP_FAILS = 5          # 5 wrong passwords from one address within WINDOW lock that address
WINDOW = 15 * 60          # for LOCKOUT. Other addresses (Roland) are not affected.
LOCKOUT = 15 * 60
GLOBAL_FAILS = 20         # After 20 wrong passwords from everyone together within WINDOW, every
GLOBAL_SLOWDOWN = 3.0     # further wrong guess is slowed down; nobody is locked out.
MAX_WAITING = 8           # Login attempts allowed to queue at once; more get "busy, try again".
# All of this is kept in memory, so restarting the agent clears it.


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def password_ok(given: str, stored_hash: str) -> bool:
    """Checks a password against the argon2 hash from .env (the password itself is never stored)."""
    if not given or not stored_hash:
        return False
    try:
        return _hasher.verify(stored_hash, given)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def client_key(ip: str) -> str:
    """The address the login limits count. One IPv6 user usually gets a whole /64 (2^64
    addresses), so IPv6 is counted per /64, not per address."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def csrf_token(session_token: str) -> str:
    """A per-session anti-CSRF token. The page reads it from /api/status and sends it back in the
    X-CSRF-Token header on every change; other sites can neither read nor guess it."""
    return hashlib.sha256(b"csrf:" + session_token.encode()).hexdigest()


class LoginLimiter:
    def __init__(self) -> None:
        self.fails: dict[str, deque[float]] = defaultdict(deque)
        self.all_fails: deque[float] = deque()
        self.locked_until: dict[str, float] = {}

    @staticmethod
    def _trim(q: deque[float], now: float) -> None:
        while q and q[0] <= now - WINDOW:
            q.popleft()

    def locked(self, ip: str, now: float | None = None) -> float:
        """Seconds until this address may try again (0 if it may try now)."""
        now = time.time() if now is None else now
        return max(0.0, self.locked_until.get(ip, 0.0) - now)

    def slowdown(self, now: float | None = None) -> float:
        """Extra seconds to hold each wrong guess while many are coming in from everywhere."""
        now = time.time() if now is None else now
        self._trim(self.all_fails, now)
        return GLOBAL_SLOWDOWN if len(self.all_fails) >= GLOBAL_FAILS else 0.0

    def failed(self, ip: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        q = self.fails[ip]
        q.append(now)
        self.all_fails.append(now)
        self._trim(q, now)
        self._trim(self.all_fails, now)
        if len(q) >= PER_IP_FAILS:
            self.locked_until[ip] = now + LOCKOUT
            q.clear()

    def succeeded(self, ip: str) -> None:
        self.fails.pop(ip, None)


class Sessions:
    def __init__(self, memory: Memory, days: int, idle_hours: float, password_hash: str):
        self.memory = memory
        self.days = days
        self.idle = idle_hours * 3600
        # A new password ends every existing login.
        fingerprint = hashlib.sha256(password_hash.encode()).hexdigest()
        if memory.get_meta("password_fingerprint") != fingerprint:
            memory.delete_all_sessions()
            memory.set_meta("password_fingerprint", fingerprint)

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        self.memory.delete_expired_sessions(now)
        self.memory.add_session(token_hash(token), now + self.days * 86400)
        return token

    def valid(self, token: str | None) -> bool:
        return bool(token) and self.memory.session_valid(token_hash(token), time.time(), self.idle)

    def end(self, token: str | None) -> None:
        if token:
            self.memory.delete_session(token_hash(token))


class LoginGate:
    """Runs one password check at a time, so lockout counts can't be raced and parallel argon2
    checks (64 MiB each) can't exhaust memory. Only MAX_WAITING attempts may queue, and each
    address (IPv6 /64) may have only one attempt in the queue at a time. Nothing waits or sleeps
    while holding the lock, so a queue of wrong guesses only delays Roland by the checks."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.waiting = 0
        self.inflight: set[str] = set()
