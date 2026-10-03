"""Login for the web page: one password, server-side sessions, and a lockout on wrong guesses."""

from __future__ import annotations

import hashlib
import secrets
import time
from collections import defaultdict, deque

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from ..memory import Memory

_hasher = PasswordHasher()  # argon2id with the library's recommended settings

COOKIE = "agent_session"

PER_IP_FAILS = 5          # wrong passwords from one address ...
GLOBAL_FAILS = 20         # ... or from everyone together ...
WINDOW = 15 * 60          # ... within 15 minutes ...
LOCKOUT = 15 * 60         # ... lock logins for 15 minutes.


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


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class LoginLimiter:
    def __init__(self) -> None:
        self.fails: dict[str, deque[float]] = defaultdict(deque)
        self.all_fails: deque[float] = deque()
        self.locked_until: dict[str, float] = {}
        self.global_locked_until = 0.0

    @staticmethod
    def _trim(q: deque[float], now: float) -> None:
        while q and q[0] <= now - WINDOW:
            q.popleft()

    def locked(self, ip: str, now: float | None = None) -> float:
        """Seconds until this address may try again (0 if it may try now)."""
        now = time.time() if now is None else now
        until = max(self.locked_until.get(ip, 0.0), self.global_locked_until)
        return max(0.0, until - now)

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
        if len(self.all_fails) >= GLOBAL_FAILS:
            self.global_locked_until = now + LOCKOUT
            self.all_fails.clear()

    def succeeded(self, ip: str) -> None:
        self.fails.pop(ip, None)


class Sessions:
    def __init__(self, memory: Memory, days: int):
        self.memory = memory
        self.days = days

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        self.memory.delete_expired_sessions(now)
        self.memory.add_session(token_hash(token), now + self.days * 86400)
        return token

    def valid(self, token: str | None) -> bool:
        return bool(token) and self.memory.session_valid(token_hash(token), time.time())

    def end(self, token: str | None) -> None:
        if token:
            self.memory.delete_session(token_hash(token))
