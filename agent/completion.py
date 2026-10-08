"""Recognise file-save acknowledgements that need evidence from this run.

This check never grants permission or writes a file. It only withholds an unverified
completion claim; the model must still choose a tool and go through the normal gate.
"""

from __future__ import annotations

import re

from .workspace import normalize

_FILE = re.compile(r"[\w./-]+\.(?:txt|md|json|csv|log|yaml|yml|html|py|js|sh)\b", re.IGNORECASE)
_QUOTED_FILE = re.compile(r"[`\"']([^`\"'\r\n]+\.[\w-]+)[`\"']")
_FILE_CONTEXT = re.compile(r"\b(?:file|workspace)\b|\b(?:save|write|create)\b.*\bnote\b", re.IGNORECASE)
_SAVE_CLAIM = re.compile(
    r"^\s*(?:saved\b|written\b|created\b|done[.!]?\s*$|"
    r"I(?: have|'ve)? (?:successfully )?(?:saved|written|created)\b|"
    r"(?:the|your) (?:file|note)\b.{0,100}\b(?:is|was|has been) (?:saved|written|created)\b)",
    re.IGNORECASE,
)


def file_context(request: str) -> bool:
    return bool(_FILE.search(request) or _FILE_CONTEXT.search(request))


def file_paths(text: str) -> set[str]:
    paths = {match.group(1) for match in _QUOTED_FILE.finditer(text)}
    unquoted = _QUOTED_FILE.sub("", text)
    return paths | {match.group() for match in _FILE.finditer(unquoted)}


def canonical_path(path: str) -> str:
    try:
        return "/".join(normalize(path))
    except ValueError:
        return ""


def unverified_save_claim(reply: str, request: str, written: set[str]) -> bool:
    if not file_context(request) or not _SAVE_CLAIM.search(reply.replace("’", "'")):
        return False
    # Explicit paths in the acknowledgement take precedence over input-file names.
    named = file_paths(reply)
    if named:
        return not all(canonical_path(path) in written for path in named)
    named = file_paths(request)
    if named:
        return not any(canonical_path(path) in written for path in named) and not any(
            path in request for path in written
        )
    return not written


UNSAVED_REPLY = "I couldn't confirm that the requested file was saved. Please check the Files tab."
