from __future__ import annotations

import json
import math
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .. import __version__


# Shared with the other sync providers; re-exported here for existing imports.
from ..import_identity import (  # noqa: E402
    IMPORT_EVENT_ID_MAX_LENGTH,
    ImportContext,
    ImportIdentityError,
    canonical_import_event_id,
)


class PortkeyConversionError(ValueError):
    """Raised when a row cannot be converted without losing its identity.

    Subclasses ValueError so it is never swallowed by the per-row malformed-data
    skip path: invalid event ids and timestamps must fail the whole window.
    """


def _tool_call_name(call: Any) -> str | None:
    if not isinstance(call, dict):
        return None
    if isinstance(call.get("name"), str):
        return call["name"]
    function = call.get("function")
    if isinstance(function, dict) and isinstance(function.get("name"), str):
        return function["name"]
    if isinstance(call.get("type"), str):
        return call["type"]
    return None


def _tool_names(tool_calls: list | None) -> list[str] | None:
    if not tool_calls:
        return None
    names = [name for call in tool_calls if (name := _tool_call_name(call))]
    return names or None


def _extract_response(response: dict) -> tuple[str | None, list | None]:
    if response.get("object") == "response" and isinstance(
        response.get("output"), list
    ):
        text_parts: list[str] = []
        tool_calls: list[Any] = []
        for item in response["output"]:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "message":
                for block in item.get("content") or []:
                    if isinstance(block, dict) and isinstance(block.get("text"), str):
                        text_parts.append(block["text"])
            else:
                tool_calls.append(item)
        return ("\n".join(text_parts) or None), (tool_calls or None)

    choices = response.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        message = message if isinstance(message, dict) else {}
        response_text = message.get("content")
        tool_calls = message.get("tool_calls")
        return (
            response_text if isinstance(response_text, str) else None,
            tool_calls if isinstance(tool_calls, list) and tool_calls else None,
        )

    content = response.get("content")
    if (
        response.get("object") is None
        and choices is None
        and isinstance(content, list)
        and content
    ):
        text_parts = []
        tool_calls = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text_parts.append(block["text"])
            elif block.get("type") == "tool_use":
                tool_calls.append(block)
        return ("\n".join(text_parts) or None), (tool_calls or None)

    return json.dumps(response), None



_NUMERIC_TIMESTAMP_RE = re.compile(
    r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z"
)
_RFC3339_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ].+\Z")
_PORTKEY_DATE_RE = re.compile(
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) "
    r"(?P<month>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
    r"(?P<day>\d{2}) (?P<year>\d{4}) "
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2}) "
    r"GMT(?P<offset_sign>[+-])(?P<offset_hour>\d{2})(?P<offset_minute>\d{2}) "
    r"\([^)]+\)\Z"
)
_MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}


def _canonical_import_event_id(raw: Any) -> str:
    """Validate a Portkey row id for use as ``import_event_id``.

    Delegates to the shared validator; the error is re-raised as a
    PortkeyConversionError so it fails the window like every other Portkey
    conversion problem.
    """
    try:
        return canonical_import_event_id(raw)
    except ImportIdentityError as exc:
        raise PortkeyConversionError(str(exc)) from exc


def _canonical_timestamp(raw: Any) -> str:
    """Return a provider timestamp as RFC 3339 UTC or fail the import window."""
    if isinstance(raw, bool) or raw is None:
        raise PortkeyConversionError("created_at must be a timestamp")

    if isinstance(raw, str):
        value = raw.strip()
        if not value:
            raise PortkeyConversionError("created_at must be a timestamp")

        if _NUMERIC_TIMESTAMP_RE.fullmatch(value):
            numeric = float(value)
            parsed = _timestamp_from_epoch(numeric, raw)
        elif _RFC3339_TIMESTAMP_RE.fullmatch(value):
            parsed = _timestamp_from_rfc3339(value, raw)
        else:
            match = _PORTKEY_DATE_RE.fullmatch(value)
            if match is None:
                raise PortkeyConversionError(
                    f"created_at is not a valid timestamp: {raw!r}"
                )
            parsed = _timestamp_from_portkey_date(match, raw)
    elif isinstance(raw, (int, float)):
        parsed = _timestamp_from_epoch(float(raw), raw)
    else:
        raise PortkeyConversionError(
            f"created_at must be a timestamp, got {type(raw).__name__}"
        )

    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp_from_rfc3339(value: str, raw: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PortkeyConversionError(
            f"created_at is not a valid timestamp: {raw!r}"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PortkeyConversionError("created_at must include a timezone offset")
    return parsed


def _timestamp_from_portkey_date(match: re.Match[str], raw: Any) -> datetime:
    offset_hour = int(match["offset_hour"])
    offset_minute = int(match["offset_minute"])
    if offset_hour > 23 or offset_minute > 59:
        raise PortkeyConversionError(
            f"created_at is not a valid timestamp: {raw!r}"
        )
    offset = timedelta(hours=offset_hour, minutes=offset_minute)
    if match["offset_sign"] == "-":
        offset = -offset
    try:
        return datetime(
            int(match["year"]),
            _MONTHS[match["month"]],
            int(match["day"]),
            int(match["hour"]),
            int(match["minute"]),
            int(match["second"]),
            tzinfo=timezone(offset),
        )
    except ValueError as exc:
        raise PortkeyConversionError(
            f"created_at is not a valid timestamp: {raw!r}"
        ) from exc


def _timestamp_from_epoch(numeric: float, raw: Any) -> datetime:
    if not math.isfinite(numeric):
        raise PortkeyConversionError("created_at must be a finite timestamp")
    if abs(numeric) >= 100_000_000_000:
        numeric /= 1000
    try:
        return datetime.fromtimestamp(numeric, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise PortkeyConversionError(
            f"created_at is outside the supported range: {raw!r}"
        ) from exc


def normalize_portkey_row(
    row: dict, *, import_context: ImportContext | None = None
) -> dict:
    import_event_id = None
    if import_context is not None:
        # Validate the imported id before anything else so a bad id fails the
        # window cleanly (row.get avoids a KeyError being swallowed as a skip).
        import_event_id = _canonical_import_event_id(row.get("id"))
    ts = _canonical_timestamp(row.get("created_at"))
    request_id = row["id"]
    trace_id = row["trace_id"]

    response = row.get("response") if isinstance(row.get("response"), dict) else {}
    status_code = row.get("response_status_code")
    is_error = not isinstance(status_code, int) or status_code >= 400
    error_type = None
    if is_error:
        err = response.get("error")
        if isinstance(err, str):
            error_type = err
        elif isinstance(err, dict):
            message = err.get("message")
            error_type = message if isinstance(message, str) else json.dumps(err)
        elif err is not None:
            error_type = json.dumps(err)

    response_text, tool_calls = _extract_response(response)
    tool_names = _tool_names(tool_calls)

    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    workflow_name = metadata.get("workflow_name")
    route = (
        workflow_name
        if isinstance(workflow_name, str) and workflow_name
        else "portkey/backfill"
    )

    cost = row.get("cost")
    cost_usd = cost / 100 if isinstance(cost, (int, float)) else None

    result = {
        "ts": ts,
        "provider": row.get("ai_org"),
        "model": row.get("ai_model"),
        "status": "error" if is_error else "success",
        "input_tokens": row.get("req_units"),
        "output_tokens": row.get("res_units"),
        "latency_ms": row.get("response_time"),
        "error": is_error,
        "error_type": error_type,
        "cost_usd": cost_usd,
        "request_id": request_id,
        "span_id": request_id,
        "trace_id": trace_id,
        "route": route,
        "tags": metadata,
        "request_json": json.dumps(row.get("request")),
        "response_text": response_text,
        "tool_calls": tool_calls,
        "tool_names": tool_names,
        "sdk": "metergraphrelay",
        "sdk_version": __version__,
        "content_opted_in": True,
    }
    if import_context is not None:
        result["import_source"] = import_context.source
        result["import_source_scope"] = import_context.source_scope
        result["import_event_id"] = import_event_id  # validated, stripped id
    return result


def convert_portkey_export(
    input_path: str,
    output_path: str,
    *,
    import_context: ImportContext | None = None,
    on_progress: Callable[[], None] | None = None,
) -> tuple[int, int]:
    """Normalize a Portkey export JSONL to MeterGraph rows, returning (converted, skipped).

    ``on_progress``, if given, is invoked once per processed (non-blank) line —
    converted or skipped — so a caller can renew a lease during a long
    normalization. Any exception it raises propagates (a lost lease must abort the
    conversion). It defaults to ``None`` so manual mode and existing callers are
    unchanged.
    """
    converted = 0
    skipped = 0
    with open(input_path) as src, open(output_path, "w") as dst:
        for line_number, line in enumerate(src, start=1):
            line = line.strip()
            if not line:
                continue
            if on_progress is not None:
                on_progress()
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                skipped += 1
                print(
                    f"Warning: skipping malformed row at line {line_number}: {exc}",
                    file=sys.stderr,
                )
                continue
            try:
                normalized = normalize_portkey_row(row, import_context=import_context)
                serialized = json.dumps(normalized)
            except (KeyError, TypeError, AttributeError) as exc:
                skipped += 1
                row_id = (
                    row.get("id", "<unknown>") if isinstance(row, dict) else "<unknown>"
                )
                print(
                    f"Warning: skipping malformed row {row_id} (line {line_number}): {exc}",
                    file=sys.stderr,
                )
                continue
            dst.write(serialized + "\n")
            converted += 1
    return converted, skipped
