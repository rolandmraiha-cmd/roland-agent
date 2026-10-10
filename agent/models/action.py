"""Action JSON Schema for constrained tool calling (llama.cpp grammar subset)."""

from __future__ import annotations

from typing import Any

# Keywords llama.cpp's JSON-schema-to-grammar converter accepts (§6.9.3).
SUPPORTED_KEYS = frozenset(
    {
        "type",
        "properties",
        "required",
        "enum",
        "const",
        "maxLength",
        "minimum",
        "maximum",
        "items",
        "additionalProperties",
        "oneOf",
        "description",
        "title",
        "minLength",
        "minItems",
        "maxItems",
    }
)


def _scrub(node: Any) -> Any:
    """Keep only the supported schema subset; force additionalProperties false on objects."""
    if isinstance(node, list):
        return [_scrub(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key not in SUPPORTED_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            # Property names are not schema keywords — scrub each child schema only.
            out[key] = {name: _scrub(schema) for name, schema in value.items()}
        else:
            out[key] = _scrub(value)
    if out.get("type") == "object" or "properties" in out:
        out["additionalProperties"] = False
    return out


def tool_parameters(schema: dict) -> dict:
    """Extract and scrub a tool's parameter object schema from an OpenAI-style tool entry."""
    params = schema.get("function", schema).get("parameters", {"type": "object", "properties": {}})
    scrubbed = _scrub(params)
    if scrubbed.get("type") != "object":
        scrubbed = {"type": "object", "properties": {}, "additionalProperties": False}
    scrubbed.setdefault("properties", {})
    scrubbed["additionalProperties"] = False
    return scrubbed


def build_action_schema(tools: list[dict]) -> dict:
    """oneOf reply + one tool shape per offered tool."""
    variants: list[dict] = [
        {
            "type": "object",
            "properties": {
                "action": {"const": "reply"},
                "text": {"type": "string"},
            },
            "required": ["action", "text"],
            "additionalProperties": False,
        }
    ]
    for entry in tools:
        fn = entry.get("function", entry)
        name = fn["name"]
        variants.append(
            {
                "type": "object",
                "properties": {
                    "action": {"const": "tool"},
                    "tool": {"const": name},
                    "args": tool_parameters(entry),
                },
                "required": ["action", "tool", "args"],
                "additionalProperties": False,
            }
        )
    return {"oneOf": variants}


def assert_schema_subset(node: Any, path: str = "$") -> None:
    """Raise ValueError if a tool schema uses unsupported keywords."""
    if isinstance(node, list):
        for i, item in enumerate(node):
            assert_schema_subset(item, f"{path}[{i}]")
        return
    if not isinstance(node, dict):
        return
    for key, value in node.items():
        if key not in SUPPORTED_KEYS:
            raise ValueError(f"unsupported schema keyword {key!r} at {path}")
        if key == "properties" and isinstance(value, dict):
            for prop, child in value.items():
                assert_schema_subset(child, f"{path}.properties.{prop}")
            continue
        assert_schema_subset(value, f"{path}.{key}")
