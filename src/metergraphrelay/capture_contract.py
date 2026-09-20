"""The capture contract metergraph's analysis pipeline enforces on pushed rows.

The authority is metergraph-pipeline's ``trace_classification/capture_normalizer``
(``_tool_calls`` and ``_normalize_result``). A row that breaks these rules is
marked unusable and dropped before classification, so it still bills and still
counts toward the population while contributing nothing to any analysis. The
failure is silent: the importer succeeds and the traffic simply never appears.

Two rules bind an importer:

* A tool call is ``{call_id, name, arguments}`` -- metergraph's SDK shape, with
  ``arguments`` a JSON string. It is deliberately not the wire shape of any
  provider API. The one exception is ``web_search_call``/``web_fetch_call``,
  which the pipeline reads as native-search audit and passes over.
* ``response_text`` is a string. A response carrying no text is ``""``; ``None``
  means the result itself is malformed.
* A numeric the source never recorded is absent, not ``None``. The pipeline
  validates ``latency_ms``, ``input_tokens`` and ``output_tokens`` whenever the
  key is present, so an explicit ``None`` reads as a malformed number.

Provider wire items that are neither a tool call nor search audit -- OpenAI
``reasoning`` items are the case seen in production -- have no representation in
the contract. They are dropped rather than passed through, because one
unconvertible element invalidates the whole row and takes the response with it.
"""

from __future__ import annotations

import json
from typing import Any

# Read by the pipeline as evidence that a provider ran search natively, and
# exempt from the tool-call shape. Passed through with their fields intact.
NATIVE_SEARCH_AUDIT_TYPES = frozenset({"web_search_call", "web_fetch_call"})

# Validated by the pipeline on presence, not on value: carrying one of these as
# an explicit None says "this is the number" and fails, where leaving it out
# says "the source did not record one" and is accepted.
PRESENCE_CHECKED_NUMERIC_FIELDS = ("latency_ms", "input_tokens", "output_tokens")


def capture_text(parts: list[str]) -> str:
    """Join a response's text parts into the contract's ``response_text``.

    Returns ``""`` for a response with no text -- a tool-only reply is a valid
    response, and ``None`` would mark the whole result malformed.
    """
    return "\n".join(parts)


def capture_response_text(value: Any, *, content_opted_in: bool = True) -> str | None:
    """A row's ``response_text``, as the contract requires it.

    ``None`` is reserved for a row carrying no content at all, which the
    pipeline recognises as the opt-out. On a row whose content *was* captured it
    means something else entirely -- a malformed result -- and takes the row out
    of every analysis. A reply with nothing to say is ``""``.
    """
    if not content_opted_in:
        return None
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value)


def _arguments(value: Any) -> str:
    """Render a tool call's arguments as the contract's JSON string."""
    if isinstance(value, str):
        return value
    if value is None:
        return "{}"
    return json.dumps(value)


def capture_tool_call(item: Any, *, position: int | None = None) -> dict | None:
    """Convert one provider tool item to the contract shape.

    Returns the item unchanged when it is native-search audit, and ``None`` for
    anything with no contract representation, which the caller drops.

    ``position`` supplies a fallback identity for a recognised tool call whose
    source recorded no id. Providers that log whatever an integration handed
    them do occasionally omit it, and dropping the call would describe a tool
    turn as though the model had replied with text alone. The contract needs the
    id only to tell one call from another within a response, which the position
    does. A call with no name is still dropped: there is nothing to identify.
    """
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    if isinstance(item_type, str) and item_type in NATIVE_SEARCH_AUDIT_TYPES:
        return item

    # Anthropic Messages: {"type": "tool_use", "id", "name", "input"}.
    if item_type == "tool_use":
        call_id, name, arguments = item.get("id"), item.get("name"), item.get("input")
    # OpenAI Responses: {"type": "function_call", "call_id", "name", "arguments"}.
    # ``call_id`` is the one the model echoes back; ``id`` is the output item's.
    elif item_type == "function_call":
        call_id = item.get("call_id") or item.get("id")
        name, arguments = item.get("name"), item.get("arguments")
    # OpenAI Chat Completions: {"id", "type": "function", "function": {...}}.
    elif item_type == "function" or isinstance(item.get("function"), dict):
        function = item.get("function")
        if not isinstance(function, dict):
            return None
        call_id = item.get("id") or item.get("call_id")
        name, arguments = function.get("name"), function.get("arguments")
    # Already in the contract shape: an importer that has nothing to convert.
    elif isinstance(item.get("call_id"), str) and isinstance(item.get("name"), str):
        call_id, name, arguments = item["call_id"], item["name"], item.get("arguments")
    else:
        return None

    if not isinstance(name, str) or not name.strip():
        return None
    if not isinstance(call_id, str) or not call_id.strip():
        if position is None:
            return None
        call_id = f"tool-{position}"
    return {"call_id": call_id, "name": name, "arguments": _arguments(arguments)}


def capture_tool_calls(items: Any) -> list | None:
    """Convert a provider's tool-call list; ``None`` when nothing survives."""
    if not isinstance(items, (list, tuple)):
        return None
    converted = [
        call
        for position, item in enumerate(items)
        if (call := capture_tool_call(item, position=position)) is not None
    ]
    return converted or None


def capture_row(row: dict) -> dict:
    """Drop the numerics a source never recorded, so the row stays readable.

    Every other field is passed through untouched.
    """
    return {
        key: value
        for key, value in row.items()
        if value is not None or key not in PRESENCE_CHECKED_NUMERIC_FIELDS
    }
