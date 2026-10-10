"""Recognise file-save acknowledgements that need evidence from this run.

This check never grants permission or writes a file. It only withholds an unverified
completion claim; the model must still choose a tool and go through the normal gate.
"""

from __future__ import annotations

import re

from .workspace import normalize

_TOKENS = re.compile(r"`[^`\r\n]*`|\"[^\"\r\n]*\"|'[^'\r\n]*'|,|[^\s,]+")
_WRITE_VERBS = frozenset({"save", "write", "create", "append"})
_DESTINATIONS = frozenset({"named", "called", "to", "into", "in", "as", "at"})
_CONTENT_START = frozenset({"with", "containing", "content", "contents", "exactly", "then", "after"})
_METADATA = frozenset({
    "a", "an", "the", "this", "that", "these", "those", "it", "them", "file", "files", "note", "notes",
    "workspace", "new", "requested", "following", "once", "twice", "again", "only", "both",
})
_FILE_WORDS = frozenset({"file", "files", "workspace", "note", "notes"})
_MEMORY_WORDS = frozenset({"fact", "facts", "preference", "preferences", "memory", "remember"})
_SAVE_CLAIM = re.compile(r"\b(?:saved|written|created)\b|^\s*done[.!]?\s*$", re.IGNORECASE)
_NEGATED = re.compile(
    r"\b(?:not|never|cannot|can't|couldn't|wasn't|isn't|haven't|hasn't|didn't|don't|no)\b", re.IGNORECASE,
)


def file_save_request(request: str) -> bool:
    tokens = _tokens(request)
    if not any(not quoted and value.lower() in _WRITE_VERBS for value, quoted in tokens):
        return False
    end = next((i for i, (v, q) in enumerate(tokens) if not q and v.lower() in _CONTENT_START), len(tokens))
    intent_words = {v.lower() for v, q in tokens[:end] if not q}
    if intent_words & _FILE_WORDS:
        return True
    if intent_words & _MEMORY_WORDS:
        return False
    return bool(_write_targets(tokens))


def _tokens(text: str) -> list[tuple[str, bool]]:
    tokens = []
    for match in _TOKENS.finditer(text):
        token = match.group()
        quoted = len(token) >= 2 and token[0] in "`\"'" and token[-1] == token[0]
        value = token[1:-1] if quoted else token.rstrip(".!?:;")
        if value:
            tokens.append((value, quoted))
    return tokens


def canonical_path(path: str) -> str:
    try:
        return "/".join(normalize(path))
    except ValueError:
        return ""


def _targets(tokens: list[tuple[str, bool]]) -> set[str]:
    """Extract explicit destinations, keeping quoted/spaced names and arbitrary extensions."""
    end = next((i for i, (v, q) in enumerate(tokens) if not q and v.lower() in _CONTENT_START), len(tokens))
    tokens = tokens[:end]
    destinations = [i for i, (v, q) in enumerate(tokens) if not q and v.lower() in _DESTINATIONS]
    named = False
    if destinations:
        start = destinations[-1]
        named = tokens[start][0].lower() in {"named", "called"}
        tokens = tokens[start + 1:]
    elif tokens and tokens[0][0].lower() in {"a", "an"} and not any(
        q or "." in v or "/" in v or v.lower() in {"file", "note"} for v, q in tokens
    ):
        return set()  # “Write a paragraph” asks for chat text, not a filename.
    groups = [[]]
    for value, quoted in tokens:
        if not quoted and value.lower() in {"and", ","}:
            groups.append([])
        elif quoted or value.lower() not in _METADATA:
            groups[-1].append((value, quoted))
    paths = set()
    for group in groups:
        if len(group) == 1 or (named and group):
            path = canonical_path(" ".join(v for v, _q in group))
            if path:
                paths.add(path)
        else:
            for value, quoted in group:
                if quoted or "." in value or "/" in value:
                    path = canonical_path(value)
                    if path:
                        paths.add(path)
    return paths


def _write_targets(tokens: list[tuple[str, bool]]) -> set[str]:
    targets = set()
    for i, (value, quoted) in enumerate(tokens):
        if quoted or value.lower() not in _WRITE_VERBS:
            continue
        end = next((j for j in range(i + 1, len(tokens)) if not tokens[j][1] and
                    tokens[j][0].lower() in _WRITE_VERBS | {"read"}), len(tokens))
        targets |= _targets(tokens[i + 1:end])
    return targets


def _claims(reply: str) -> list[tuple[str, int, int]]:
    claims = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", reply.replace("’", "'")):
        for match in _SAVE_CLAIM.finditer(sentence):
            # A negated clause is an honest failure, not an acknowledgement.
            prefix = re.split(r"[;,]|\bbut\b", sentence[:match.start()], flags=re.IGNORECASE)[-1]
            if _NEGATED.search(prefix):
                continue
            following = sentence[match.end():].lstrip().lower()
            if following.startswith(("means", "refers", "indicates")):
                continue  # explaining the UI word, not claiming a write
            claims.append((sentence, match.start(), match.end()))
            break
    return claims


def unverified_save_claim(reply: str, request: str, written: set[str]) -> bool:
    if not file_save_request(request):
        return False
    required = _write_targets(_tokens(request))
    for claim, start, end in _claims(reply):
        named = _targets(_tokens(claim[end:]))
        if not named:
            # Passive acknowledgements can name the target before “has been saved”.
            named = {canonical_path(v) for v, q in _tokens(claim[:start]) if q or "." in v or "/" in v}
            named.discard("")
        expected = named or required
        if not written or (expected and not expected.issubset(written)):
            return True
    return False


UNSAVED_REPLY = "I couldn't confirm that the requested file was saved. Please check the Files tab."
