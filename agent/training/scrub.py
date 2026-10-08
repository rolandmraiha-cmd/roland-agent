"""Linear, bounded privacy filters applied at capture AND export (§6.11.2)."""

from __future__ import annotations

import datetime
import re
from collections import Counter

CHECKS = "0123456789ABCDEFHJKLMNPRSTUVWXY"


def luhn(value: str) -> bool:
    digits = [int(char) for char in value if char.isdigit() and char.isascii()]
    if not 13 <= len(digits) <= 19:
        return False
    total = sum(
        (digit * 2 - 9 if digit >= 5 else digit * 2) if i % 2 else digit
        for i, digit in enumerate(reversed(digits))
    )
    return total % 10 == 0


def iban(value: str) -> bool:
    text = value.replace(" ", "").upper()
    if not 15 <= len(text) <= 34:
        return False
    rotated = text[4:] + text[:4]
    numeric = "".join(str(ord(char) - 55) if char.isalpha() else char for char in rotated)
    return numeric.isdecimal() and int(numeric) % 97 == 1


def hetu(value: str) -> bool:
    try:
        century = 1800 if value[6] == "+" else 1900 if value[6] in "-UVWXY" else 2000
        datetime.date(century + int(value[4:6]), int(value[2:4]), int(value[:2]))
        return CHECKS[int(value[:6] + value[7:10]) % 31] == value[-1]
    except (ValueError, IndexError):
        return False


PATTERNS = [
    (
        "credential",
        re.compile(
            r"\b(?:sk-|xai-|ghp_|gho_|github_pat_|xox[abpr]-|glpat-)[A-Za-z0-9_-]{16,256}|\bAKIA[A-Z0-9]{16}\b|\bAIza[A-Za-z0-9_-]{35}\b"
        ),
        "[CREDENTIAL]",
        None,
    ),
    (
        "credential",
        re.compile(
            r"(?i)\b(?:key|token|secret|password|passwd|pwd)\s{0,8}[:=]\s{0,8}[A-Za-z0-9+/=_-]{32,4096}"
        ),
        "[CREDENTIAL]",
        None,
    ),
    (
        "credential",
        re.compile(
            r"(?<![A-Za-z0-9_.-])[A-Za-z0-9_-]{8,1365}\.[A-Za-z0-9_-]{8,1365}\.[A-Za-z0-9_-]{8,1364}(?![A-Za-z0-9_.-])"
        ),
        "[CREDENTIAL]",
        None,
    ),
    ("header", re.compile(r"(?im)\b(?:Authorization|Cookie|Set-Cookie):[^\r\n]{0,8192}"), "[REDACTED]", None),
    (
        "query",
        re.compile(r"(?i)([?&](?:token|key|sig|signature|code|password|session|auth)=)[^&#\s]{0,8192}"),
        r"\1[REDACTED]",
        None,
    ),
    ("iban", re.compile(r"\b[A-Z]{2}[0-9]{2}(?: ?[A-Z0-9]){11,30}\b"), "[IBAN]", iban),
    ("hetu", re.compile(r"\b[0-9]{6}[-+A-FU-Y][0-9]{3}[0-9A-Y]\b"), "[HETU]", hetu),
    ("card", re.compile(r"(?<![0-9])(?:[0-9][ -]?){12,18}[0-9](?![0-9])"), "[CARD]", luhn),
    (
        "phone",
        re.compile(r"(?<![\w+])(?:\+[1-9][0-9 -]{6,18}[0-9]|0(?:4[0-9]|50)[0-9 -]{4,14}[0-9])(?!\w)"),
        "[PHONE]",
        None,
    ),
    (
        "email",
        re.compile(
            r"(?<![A-Za-z0-9.!#$%&'*+/=?^_`{|}~-])[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}"
        ),
        "[EMAIL]",
        None,
    ),
]


class Scrubber:
    def __init__(self, secrets=(), keep_emails=()):
        self.secrets = tuple(sorted({str(s) for s in secrets if s}, key=len, reverse=True))
        self.keep_emails = {s.casefold() for s in keep_emails}

    def text(self, value: str, counts: Counter) -> str:
        for secret in self.secrets:
            counts["secret"] += value.count(secret)
            value = value.replace(secret, "[SECRET]")
        # Split at BEGIN markers rather than searching from every possible prefix.
        parts = value.split("-----BEGIN ")
        output = [parts[0]]
        for part in parts[1:]:
            heading, _, rest = part.partition("-----")
            if "PRIVATE KEY" in heading[:80]:
                _, end, after = rest.partition("-----END " + heading + "-----")
                output.append("[PRIVATE_KEY]" + (after if end else ""))
                counts["private_key"] += 1
            else:
                output.append("-----BEGIN " + part)
        value = "".join(output)
        for category, pattern, replacement, check in PATTERNS:

            def redact(match, category=category, replacement=replacement, check=check):
                if check is not None and not check(match.group()):
                    if category == "iban":
                        # A following uppercase word can be consumed by the bounded BBAN
                        # pattern. Try only space boundaries, longest first, never each offset.
                        text = match.group()
                        for index in reversed([i for i, char in enumerate(text) if char == " "]):
                            if iban(text[:index]):
                                counts[category] += 1
                                return "[IBAN]" + text[index:]
                    return match.group()
                if category == "email" and match.group().casefold() in self.keep_emails:
                    return match.group()
                counts[category] += 1
                return match.expand(replacement)

            value = pattern.sub(redact, value)
        return value

    def scrub(self, value) -> tuple[object, dict]:
        counts = Counter()

        def walk(item):
            if isinstance(item, str):
                return self.text(item, counts)
            if isinstance(item, list):
                return [walk(child) for child in item]
            if isinstance(item, dict):
                result = {}
                for key, child in item.items():
                    clean_key = self.text(str(key), counts)
                    if str(key).casefold() in {"password", "salasana"}:
                        counts["password"] += 1
                        result[clean_key] = "[PASSWORD]"
                    else:
                        result[clean_key] = walk(child)
                return result
            return item

        return walk(value), dict(counts)

    def check(self, value) -> None:
        from .files import encode

        # Inspect strings before JSON escaping, too, including newline-containing secrets.
        def strings(item):
            if isinstance(item, str):
                yield item
            elif isinstance(item, list):
                for child in item:
                    yield from strings(child)
            elif isinstance(item, dict):
                for key, child in item.items():
                    yield str(key)
                    yield from strings(child)

        if any(secret in text for text in strings(value) for secret in self.secrets):
            raise ValueError("Export aborted: a loaded secret survived scrubbing")
        encode(value)  # fail on unencodable data before files are published
