"""Extract provider completion reasons without guessing from token counts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_MAX_REASON_LENGTH = 128
# LangChain runs nest the reason as outputs.generations[[...]].generation_info
# or .message.kwargs.response_metadata, six levels below the run.
_MAX_DEPTH = 8
_REASON_KEYS = ("stop_reason", "finish_reason", "finishReason")
_REASON_CONTAINER_KEYS = (
    "incomplete_details",
    "response",
    "output",
    "outputs",
    "metadata",
    "invocation_params",
    "generations",
    "generation_info",
    "message",
    "kwargs",
    "response_metadata",
    "extra",
)


def _string_reason(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    reason = value.strip()
    if not reason or len(reason) > _MAX_REASON_LENGTH:
        return None
    return reason


def _reason_value(value: object) -> str | None:
    direct = _string_reason(value)
    if direct is not None:
        return direct
    if isinstance(value, (list, tuple)):
        for item in value:
            reason = _reason_value(item)
            if reason is not None:
                return reason
    return None


def _find_reason(value: object, *, depth: int = 0) -> str | None:
    if depth > _MAX_DEPTH:
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            reason = _find_reason(item, depth=depth + 1)
            if reason is not None:
                return reason
        return None
    if not isinstance(value, Mapping):
        return None

    for key in _REASON_KEYS + ("finish_reasons", "finishReasons"):
        reason = _reason_value(value.get(key))
        if reason is not None:
            return reason
    incomplete_details = value.get("incomplete_details")
    if isinstance(incomplete_details, Mapping):
        reason = _reason_value(incomplete_details.get("reason"))
        if reason is not None:
            return reason
    for key in ("choices", "candidates") + tuple(
        key for key in _REASON_CONTAINER_KEYS if key != "incomplete_details"
    ):
        reason = _find_reason(value.get(key), depth=depth + 1)
        if reason is not None:
            return reason
    return None


def extract_stop_reason(*values: Any) -> str | None:
    """Return the first known provider reason from documented response shapes."""
    for value in values:
        reason = _reason_value(value) or _find_reason(value)
        if reason is not None:
            return reason
    return None


def extract_phoenix_stop_reason(attributes: Mapping[str, Any]) -> str | None:
    """Read finish reasons from Phoenix's flattened OpenTelemetry attributes."""
    reason = extract_stop_reason(attributes)
    if reason is not None:
        return reason
    for key, value in attributes.items():
        if (
            key.startswith("llm.output_messages.")
            and key.endswith((".message.finish_reason", ".message.stop_reason"))
        ) or key in {
            "gen_ai.response.finish_reason",
            "gen_ai.response.finish_reasons",
            "llm.response.finish_reason",
            "llm.response.stop_reason",
        }:
            reason = _reason_value(value) or _find_reason(value)
            if reason is not None:
                return reason
    return None
