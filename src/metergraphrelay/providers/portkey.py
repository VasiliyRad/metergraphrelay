from __future__ import annotations

import json
import math
import re
import sys
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .. import __version__
from ..capture_contract import capture_row, capture_text, capture_tool_calls


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
    """Map a Portkey-logged provider response onto (response_text, tool_calls).

    Every branch emits the capture contract (see ``..capture_contract``), never
    the provider's wire shape: a row the pipeline cannot read is dropped from
    analysis while still counting as captured traffic.
    """
    if response.get("object") == "response" and isinstance(
        response.get("output"), list
    ):
        text_parts: list[str] = []
        raw_items: list[Any] = []
        for item in response["output"]:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "message":
                for block in item.get("content") or []:
                    if isinstance(block, dict) and isinstance(block.get("text"), str):
                        text_parts.append(block["text"])
            else:
                raw_items.append(item)
        return capture_text(text_parts), capture_tool_calls(raw_items)

    choices = response.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        message = message if isinstance(message, dict) else {}
        content = message.get("content")
        if isinstance(content, str):
            text_parts = [content]
        elif isinstance(content, list):
            # Multimodal replies carry text in blocks; a tool-only reply carries
            # no text at all, which the contract spells "".
            text_parts = [
                block["text"]
                for block in content
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            ]
        else:
            text_parts = []
        return capture_text(text_parts), capture_tool_calls(message.get("tool_calls"))

    content = response.get("content")
    if (
        response.get("object") is None
        and choices is None
        and isinstance(content, list)
        and content
    ):
        text_parts = []
        raw_items = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text_parts.append(block["text"])
            else:
                raw_items.append(block)
        return capture_text(text_parts), capture_tool_calls(raw_items)

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


# Portkey passes the upstream provider's usage block through untouched, so one
# export carries every vendor's spelling of the same counts. Each is resolved by
# path and the first present one wins. Reasoning tokens are already inside
# res_units, so they are detail only and never added to output_tokens.
_CACHE_READ_PATHS = (
    ("input_tokens_details", "cached_tokens"),
    ("prompt_tokens_details", "cached_tokens"),
    ("cache_read_input_tokens",),
)
_CACHE_WRITE_PATHS = (
    ("cache_creation_input_tokens",),
    ("input_tokens_details", "cache_write_tokens"),
)
# Some responses carry the TTL split in place of the aggregate, so summing the
# split is the only way to see the total.
_CACHE_WRITE_5M_PATH = ("cache_creation", "ephemeral_5m_input_tokens")
_CACHE_WRITE_1H_PATH = ("cache_creation", "ephemeral_1h_input_tokens")
_CACHE_WRITE_TTL_PATHS = (_CACHE_WRITE_5M_PATH, _CACHE_WRITE_1H_PATH)
_REASONING_PATHS = (
    ("output_tokens_details", "reasoning_tokens"),
    ("completion_tokens_details", "reasoning_tokens"),
)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _dig(source: Any, path: tuple[str, ...]) -> Any:
    """Follow a key path through nested dicts, returning None off the path.

    The usage block is provider-supplied and reaches us through an error path as
    readily as a success one, so any level may be missing or not a dict.
    """
    current = source
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _first_int(source: Any, paths: tuple[tuple[str, ...], ...]) -> int | None:
    for path in paths:
        value = _dig(source, path)
        if _is_int(value):
            return value
    return None


def _first_present(source: Any, paths: tuple[tuple[str, ...], ...]) -> Any:
    for path in paths:
        value = _dig(source, path)
        if value is not None:
            return value
    return None


def _sum_ints(source: Any, paths: tuple[tuple[str, ...], ...]) -> int | None:
    values = [value for path in paths if _is_int(value := _dig(source, path))]
    return sum(values) if values else None


def _usage_detail(response: dict) -> dict[str, Any]:
    """Usage detail from a Portkey row's response, present keys only.

    Absent and zero price differently, so a detail nobody reported stays absent
    rather than becoming a plausible-looking 0 that hides a capture regression.
    """
    usage = response.get("usage")
    detail: dict[str, Any] = {}

    cache_read = _first_int(usage, _CACHE_READ_PATHS)
    if cache_read is not None:
        detail["cache_read_tokens"] = cache_read

    write_5m = _dig(usage, _CACHE_WRITE_5M_PATH)
    if _is_int(write_5m):
        detail["cache_write_5m_tokens"] = write_5m
    write_1h = _dig(usage, _CACHE_WRITE_1H_PATH)
    if _is_int(write_1h):
        detail["cache_write_1h_tokens"] = write_1h

    cache_write = _first_int(usage, _CACHE_WRITE_PATHS)
    if cache_write is None:
        # Only the TTL split was reported: sum it so a consumer that reads just
        # cache_write_tokens still sees every written token.
        cache_write = _sum_ints(usage, _CACHE_WRITE_TTL_PATHS)
    if cache_write is not None:
        detail["cache_write_tokens"] = cache_write

    reasoning = _first_int(usage, _REASONING_PATHS)
    if reasoning is not None:
        detail["reasoning_tokens"] = reasoning

    # Premium tier and region are request metadata, not counts, and they are
    # what makes priority/batch and regional pricing answerable at all. The
    # ingest writer keeps unrecognised keys in calls.meta, so they survive
    # without a schema change.
    service_tier = _first_present(
        {"response": response, "usage": usage},
        (("response", "service_tier"), ("usage", "service_tier")),
    )
    if service_tier is not None:
        detail["service_tier"] = service_tier

    inference_geo = _dig(usage, ("inference_geo",))
    if inference_geo is not None:
        detail["inference_geo"] = inference_geo

    server_tool_use = _dig(usage, ("server_tool_use",))
    if isinstance(server_tool_use, dict) and server_tool_use:
        detail["server_tool_use"] = server_tool_use

    grounding_queries = _grounding_queries(response)
    if grounding_queries is not None:
        detail["grounding_queries"] = grounding_queries

    searches = _web_search_calls(response)
    if searches is not None:
        detail["web_search_calls"] = searches

    return detail


def _web_search_calls(response: dict) -> int | None:
    """How many searches the model ran, for providers that bill per search.

    The count is in the output the model produced, not in `usage`, and a single
    call runs several. It is a whole charge of its own: on this traffic it is
    priced per search at a rate the token rates cannot express.
    """
    output = response.get("output")
    if not isinstance(output, list):
        return None
    return sum(
        1
        for item in output
        if isinstance(item, dict) and item.get("type") == "web_search_call"
    )


def _endpoint(response: dict) -> str | None:
    """Which provider API the call went to, as the billing evidence names it.

    A gateway's reported cost is only interpretable alongside the endpoint that
    produced it, because the same gateway prices its endpoints differently.
    """
    if response.get("object") == "response":
        return "responses"
    if isinstance(response.get("choices"), list):
        return "chat.completions"
    return None


def _grounding_queries(response: dict) -> int | None:
    """How many grounding queries a Gemini response ran.

    Google bills grounding per query, not per prompt, and one prompt runs
    several, so the count cannot be derived from the call count. It sits outside
    `usage`, under each choice's groundingMetadata.
    """
    choices = response.get("choices")
    if not isinstance(choices, list):
        return None
    total = 0
    seen = False
    for choice in choices:
        queries = _dig(choice, ("groundingMetadata", "webSearchQueries"))
        if isinstance(queries, list):
            seen = True
            total += len(queries)
    return total if seen else None


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
    # Portkey states the cost in cents. Dividing in decimal and carrying the
    # result as a string keeps it exact: the value ends up in a numeric column,
    # and binary rounding introduced here would survive the whole way.
    reported_cost_usd = (
        str(Decimal(str(cost)) / 100) if isinstance(cost, (int, float)) else None
    )

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
        # Portkey's own figure, named so it can be recognised as a gateway's
        # amount rather than a number of unknown origin. `cost_usd` stays for
        # application versions that only read the legacy field.
        "cost_usd": cost_usd,
        "reported_cost_usd": reported_cost_usd,
        "reported_cost_source": "portkey.cost" if reported_cost_usd else None,
        "gateway": "portkey",
        "endpoint": _endpoint(response),
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
    result.update(_usage_detail(response))
    if import_context is not None:
        result["import_source"] = import_context.source
        result["import_source_scope"] = import_context.source_scope
        result["import_event_id"] = import_event_id  # validated, stripped id
    return capture_row(result)


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
