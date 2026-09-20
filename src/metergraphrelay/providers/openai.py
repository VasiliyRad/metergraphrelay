from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from itertools import islice
from typing import Any, Iterable

from .. import __version__
from ..capture_contract import capture_response_text, capture_row, capture_tool_calls


def _as_mapping(call: Any) -> Any:
    """An SDK tool-call object as a plain mapping, however the client models it."""
    if isinstance(call, dict):
        return call
    for attribute in ("model_dump", "to_dict", "dict"):
        dump = getattr(call, attribute, None)
        if callable(dump):
            try:
                return dump()
            except TypeError:
                continue
    return getattr(call, "__dict__", {})


def _usage_int(usage: Any, group: str, field: str) -> int | None:
    """One nested usage count, when the provider recorded it.

    Absent and zero are different facts: a provider reporting 0 says the feature
    was off for that call, where a missing count says nothing at all and must
    not be read as zero.
    """
    detail = getattr(usage, group, None)
    value = getattr(detail, field, None)
    if value is None and isinstance(detail, dict):
        value = detail.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _usage_detail(usage: Any) -> dict[str, int]:
    """Counts that sit beside the totals and are billed at their own rates.

    `prompt_tokens` already includes the cached tokens, so a row without the
    cache count bills them at the full input rate rather than the far cheaper
    cache rate. Reasoning tokens are already inside `completion_tokens` and are
    carried as detail only, never added to the total.
    """
    detail = {}
    cached = _usage_int(usage, "prompt_tokens_details", "cached_tokens")
    if cached is not None:
        detail["cache_read_tokens"] = cached
    reasoning = _usage_int(usage, "completion_tokens_details", "reasoning_tokens")
    if reasoning is not None:
        detail["reasoning_tokens"] = reasoning
    return detail


def normalize_completion(
    completion: Any,
    messages: Iterable[Any],
    *,
    route: str,
    include_content: bool,
    content_fetch_error: Exception | None = None,
) -> dict:
    usage = getattr(completion, "usage", None)
    tags = getattr(completion, "metadata", None) or {}
    ts = datetime.fromtimestamp(completion.created, tz=timezone.utc).isoformat()
    message_list = list(messages)

    content_opted_in = include_content and content_fetch_error is None
    request_json: str | None = None
    response_text: str | None = None
    tool_calls: list | None = None
    if content_opted_in:
        request_json = json.dumps(
            [{"role": m.role, "content": m.content} for m in message_list]
        )
        # messages.list() only ever returns the request/input messages, never
        # the model's own reply — that lives on the completion object itself.
        choices = getattr(completion, "choices", None) or []
        if choices:
            message = choices[0].message
            response_text = getattr(message, "content", None)
            # A tool-only reply carries its answer in the tool call and no text.
            # Recording the empty text without the call would describe the turn
            # as an empty response rather than the tool use it was.
            tool_calls = capture_tool_calls(
                [_as_mapping(call) for call in getattr(message, "tool_calls", None) or []]
            )

    return capture_row({
        "ts": ts,
        "provider": "openai",
        "model": completion.model,
        "status": "success",
        "endpoint": "chat.completions",
        "input_tokens": getattr(usage, "prompt_tokens", None) if usage else None,
        "output_tokens": getattr(usage, "completion_tokens", None) if usage else None,
        **_usage_detail(usage),
        "error": content_fetch_error is not None,
        "error_type": (
            type(content_fetch_error).__name__ if content_fetch_error else None
        ),
        "request_id": completion.id,
        "tags": tags,
        "route": route,
        "content_opted_in": content_opted_in,
        "request_json": request_json,
        "response_text": capture_response_text(
            response_text, content_opted_in=content_opted_in
        ),
        "tool_calls": tool_calls,
        "sdk": "metergraphrelay",
        "sdk_version": __version__,
    })


def pull_openai(
    client: Any,
    count: int,
    output_path: str,
    *,
    route: str,
    include_content: bool,
    echo_stdout: bool = False,
) -> int:
    page = client.chat.completions.list(order="desc", limit=count)
    completions = list(islice(iter(page), count))
    written = 0
    with open(output_path, "w") as f:
        for completion in completions:
            if not include_content:
                row = normalize_completion(
                    completion, [], route=route, include_content=include_content
                )
            else:
                try:
                    messages = client.chat.completions.messages.list(completion.id)
                except Exception as exc:
                    print(
                        f"Warning: could not fetch messages for {completion.id}: {exc}",
                        file=sys.stderr,
                    )
                    row = normalize_completion(
                        completion, [], route=route, include_content=include_content,
                        content_fetch_error=exc,
                    )
                else:
                    row = normalize_completion(
                        completion, messages, route=route,
                        include_content=include_content,
                    )
            line = json.dumps(row)
            f.write(line + "\n")
            if echo_stdout:
                print(line)
            written += 1
    return written
