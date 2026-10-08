"""Purpose-bound, single-use, two-minute trainer requests from a human UI decision."""

import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from uuid import uuid4

from .files import encode


def mint(secret: str, purpose: str, identifier: str, digest: str = "") -> str:
    if len(secret) < 32:
        raise ValueError("A configured trainer token is required")
    body = encode(
        {"purpose": purpose, "id": identifier, "sha256": digest, "ts": time.time(), "nonce": uuid4().hex}
    ).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(body + signature).decode()


def consume(secret: str, token: str, purpose: str, identifier: str, digest: str, root: Path) -> None:
    try:
        raw = base64.urlsafe_b64decode(token)
        body, signature = raw[:-32], raw[-32:]
        if len(raw) > 2048 or not hmac.compare_digest(
            signature, hmac.new(secret.encode(), body, hashlib.sha256).digest()
        ):
            raise ValueError("signature")
        data = json.loads(body)
        if (data["purpose"], data["id"], data["sha256"]) != (purpose, identifier, digest):
            raise ValueError("purpose")
        if not 0 <= time.time() - data["ts"] <= 120:
            raise ValueError("expired")
        nonce = data["nonce"]
        if not isinstance(nonce, str) or len(nonce) != 32 or any(c not in "0123456789abcdef" for c in nonce):
            raise ValueError("nonce")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(root / nonce, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
    except (ValueError, KeyError, TypeError, OSError) as error:
        raise PermissionError("Invalid, expired or already used human request") from error
