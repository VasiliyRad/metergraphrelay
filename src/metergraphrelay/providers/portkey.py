from __future__ import annotations

import json
import sys
from typing import Any

from .. import __version__
from ..contract import with_import_provenance


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


def normalize_portkey_row(row: dict, *, source_scope: str = "default") -> dict:
    ts = row["created_at"]
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

    normalized = {
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
    return with_import_provenance(
        normalized,
        source="portkey",
        scope=source_scope,
        event_id=request_id,
        source_trace_id=trace_id,
    )


def convert_portkey_export(
    input_path: str, output_path: str, *, source_scope: str = "default"
) -> tuple[int, int]:
    converted = 0
    skipped = 0
    with open(input_path) as src, open(output_path, "w") as dst:
        for line_number, line in enumerate(src, start=1):
            line = line.strip()
            if not line:
                continue
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
                normalized = normalize_portkey_row(row, source_scope=source_scope)
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
