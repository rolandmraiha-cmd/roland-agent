"""Parse and validate constrained action JSON from local model output."""

from __future__ import annotations

import json
import re
from typing import Any

from .base import Step, ToolCall

# Shown after a reply that reached MODEL_MAX_NEW_TOKENS. Asking the model to start again only
# produces another reply of the same length, and each attempt takes minutes on a CPU.
CUT_OFF_NOTE = '\n\n[The answer was cut off at the length limit. Send "continue" for the rest.]'

# A reply action as the schema makes the model write it: "action" first.
_REPLY_START = re.compile(r'\s*\{\s*"action"\s*:\s*"reply"\s*,')
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")

ESCAPE_MAP = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}


def _skip_ws(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t\n\r":
        i += 1
    return i


def first_object(text: str) -> str | None:
    """Linear scan for the first balanced top-level {...}."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def strip_fences(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def repair_action(data: Any) -> dict | None:
    """Map common slips onto the action shape. Returns None if unusable."""
    if not isinstance(data, dict):
        return None
    out = dict(data)
    if "action" not in out and "name" in out and ("arguments" in out or "args" in out):
        out = {
            "action": "tool",
            "tool": out["name"],
            "args": out.get("args", out.get("arguments")),
        }
    action = out.get("action")
    if action == "tool_call":
        out["action"] = "tool"
        action = "tool"
    if action == "tool":
        if "tool" not in out and "name" in out:
            out["tool"] = out["name"]
        args = out.get("args", out.get("arguments", {}))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                return None
        out["args"] = args if isinstance(args, dict) else None
        out = {"action": "tool", "tool": out.get("tool"), "args": out.get("args")}
    elif action == "reply":
        out = {"action": "reply", "text": out.get("text", "")}
    else:
        return None
    return out


def validate(schema: dict, value: Any) -> str | None:
    """Tiny validator for the supported schema subset. Returns an error string or None."""
    if "oneOf" in schema:
        errors = [validate(option, value) for option in schema["oneOf"]]
        if any(error is None for error in errors):
            return None
        return "value does not match oneOf"
    if "const" in schema:
        return None if value == schema["const"] else f"expected const {schema['const']!r}"
    if "enum" in schema:
        return None if value in schema["enum"] else "value not in enum"
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            return "expected object"
        props = schema.get("properties", {})
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                return f"missing required {key}"
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    return f"unexpected property {key}"
        for key, child in value.items():
            if key in props:
                error = validate(props[key], child)
                if error:
                    return error
        return None
    if expected == "array":
        if not isinstance(value, list):
            return "expected array"
        item_schema = schema.get("items")
        if item_schema is not None:
            for item in value:
                error = validate(item_schema, item)
                if error:
                    return error
        return None
    if expected == "string":
        if not isinstance(value, str):
            return "expected string"
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return "string too long"
        if "minLength" in schema and len(value) < schema["minLength"]:
            return "string too short"
        return None
    if expected == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return "expected integer"
        if "minimum" in schema and value < schema["minimum"]:
            return "below minimum"
        if "maximum" in schema and value > schema["maximum"]:
            return "above maximum"
        return None
    if expected == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "expected number"
        if "minimum" in schema and value < schema["minimum"]:
            return "below minimum"
        if "maximum" in schema and value > schema["maximum"]:
            return "above maximum"
        return None
    if expected == "boolean":
        return None if isinstance(value, bool) else "expected boolean"
    if expected == "null":
        return None if value is None else "expected null"
    return None


def parse_action(
    raw: str,
    schema: dict,
    offered: set[str],
    *,
    truncated: bool = False,
    call_index: int = 0,
) -> Step:
    """Parse raw model output into a Step. Never raises for bad model text."""
    if truncated:
        return Step(text=raw, parse_error="output truncated at max_tokens")
    cleaned = strip_fences(raw)
    blob = first_object(cleaned)
    if blob is None:
        return Step(text=raw, parse_error="no JSON object found")
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return Step(text=raw, parse_error="invalid JSON")
    repaired = repair_action(data)
    if repaired is None:
        return Step(text=raw, parse_error="unrecognised action shape")
    if repaired["action"] == "tool":
        name = repaired.get("tool")
        if not isinstance(name, str) or name not in offered:
            return Step(text=raw, parse_error="unknown or unoffered tool")
    error = validate(schema, repaired)
    if error:
        return Step(text=raw, parse_error=error)
    if repaired["action"] == "reply":
        text = repaired.get("text") or ""
        if not isinstance(text, str):
            return Step(text=raw, parse_error="reply text must be a string")
        return Step(text=text)
    name = repaired.get("tool")
    if not isinstance(name, str) or name not in offered:
        return Step(text=raw, parse_error="unknown or unoffered tool")
    args = repaired.get("args")
    if not isinstance(args, dict):
        return Step(text=raw, parse_error="tool args must be an object")
    return Step(
        text="",
        tool_calls=[ToolCall(id=f"call_{call_index}", name=name, arguments=json.dumps(args))],
    )


def unwrap_reply_text(text: str) -> str:
    """The model sometimes writes a whole reply action as its reply text. Keep only the text."""
    if not _REPLY_START.match(text):
        return text
    blob = first_object(text)
    if blob is not None:  # complete: let the JSON parser decode it
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("text"), str) and data["text"].strip():
            return data["text"]
    inner = TextFieldStreamer()  # cut off: decode as far as it got
    inner.feed(text)
    return inner.emitted if inner.emitted.strip() else text


def finish_step(raw: str, schema: dict, offered: set[str], *, truncated: bool, streamed: str) -> Step:
    """Turn a provider's finished output into a Step.

    `streamed` is the reply text already shown to Roland while the model wrote it. A reply cut
    off at the length limit is kept, with a note, instead of being thrown away and asked for
    again. A cut-off tool call is still an error, because its arguments are incomplete.
    """
    if truncated:
        if _REPLY_START.match(strip_fences(raw)) and streamed.strip():
            return Step(text=_whole_characters(unwrap_reply_text(streamed)).rstrip() + CUT_OFF_NOTE)
        return parse_action(raw, schema, offered, truncated=True)
    step = parse_action(raw, schema, offered)
    if step.parse_error is None and not step.tool_calls:
        if streamed:
            # Prefer streamed text if parse produced the same reply.
            step.text = streamed
        step.text = _whole_characters(unwrap_reply_text(step.text))
    return step


def _whole_characters(text: str) -> str:
    """json.loads keeps a lone half of a surrogate pair, which UTF-8 can't encode."""
    return _LONE_SURROGATE.sub("�", text)


class TextFieldStreamer:
    """Incrementally unescape the JSON string value of the top-level \"text\" field."""

    def __init__(self) -> None:
        self.buf = ""
        self._phase = "seek"  # seek | colon | value | done
        self._escape = False
        self._unicode: list[str] | None = None
        self._high: int | None = None  # first half of a 😀-style pair
        self.emitted = ""

    def _put(self, text: str, out: list[str]) -> None:
        if self._high is not None:  # a first half with no second half
            self._high = None
            out.append("�")
            self.emitted += "�"
        if text:
            out.append(text)
            self.emitted += text

    def _put_code(self, code: int, out: list[str]) -> None:
        # JSON writes characters outside the BMP (emoji) as two \u escapes. A lone half
        # can't be encoded as UTF-8 and would break the reply stream, so join or replace.
        if 0xD800 <= code <= 0xDBFF:
            self._put("", out)
            self._high = code
        elif 0xDC00 <= code <= 0xDFFF:
            if self._high is None:
                self._put("�", out)
            else:
                pair = chr(0x10000 + ((self._high - 0xD800) << 10) + (code - 0xDC00))
                self._high = None
                self._put(pair, out)
        else:
            self._put(chr(code), out)

    def feed(self, chunk: str) -> list[str]:
        self.buf += chunk
        out: list[str] = []
        i = 0
        while i < len(self.buf):
            ch = self.buf[i]
            if self._phase == "seek":
                marker = '"text"'
                pos = self.buf.find(marker, i)
                if pos < 0:
                    # Keep a short tail in case the marker straddles chunks.
                    keep = len(marker) - 1
                    self.buf = self.buf[-keep:] if keep > 0 else ""
                    return out
                i = pos + len(marker)
                self._phase = "colon"
                continue
            if self._phase == "colon":
                i = _skip_ws(self.buf, i)
                if i >= len(self.buf):
                    break
                if self.buf[i] != ":":
                    self._phase = "done"
                    break
                i += 1
                i = _skip_ws(self.buf, i)
                if i >= len(self.buf):
                    self.buf = self.buf[i:]
                    return out
                if self.buf[i] != '"':
                    self._phase = "done"
                    break
                i += 1
                self._phase = "value"
                continue
            if self._phase == "value":
                if self._unicode is not None:
                    if ch not in "0123456789abcdefABCDEF":
                        self._phase = "done"
                        break
                    self._unicode.append(ch)
                    i += 1
                    if len(self._unicode) == 4:
                        code = int("".join(self._unicode), 16)
                        self._unicode = None
                        self._put_code(code, out)
                    continue
                if self._escape:
                    self._escape = False
                    if ch == "u":
                        self._unicode = []
                        i += 1
                        continue
                    mapped = ESCAPE_MAP.get(ch)
                    if mapped is None:
                        self._phase = "done"
                        break
                    self._put(mapped, out)
                    i += 1
                    continue
                if ch == "\\":
                    self._escape = True
                    i += 1
                    continue
                if ch == '"':
                    self._put("", out)  # a first half right before the end
                    self._phase = "done"
                    i += 1
                    break
                self._put(ch, out)
                i += 1
                continue
            break
        self.buf = self.buf[i:]
        return out
