from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from itertools import islice
from typing import Any, Iterable

from .. import __version__
from ..contract import with_import_provenance


def normalize_completion(
    completion: Any,
    messages: Iterable[Any],
    *,
    route: str,
    include_content: bool,
    content_fetch_error: Exception | None = None,
    source_scope: str = "default",
) -> dict:
    usage = getattr(completion, "usage", None)
    tags = getattr(completion, "metadata", None) or {}
    ts = datetime.fromtimestamp(completion.created, tz=timezone.utc).isoformat()
    message_list = list(messages)

    content_opted_in = include_content and content_fetch_error is None
    request_json: str | None = None
    response_text: str | None = None
    if content_opted_in:
        request_json = json.dumps(
            [{"role": m.role, "content": m.content} for m in message_list]
        )
        # messages.list() only ever returns the request/input messages, never
        # the model's own reply — that lives on the completion object itself.
        choices = getattr(completion, "choices", None) or []
        if choices:
            response_text = getattr(choices[0].message, "content", None)

    row = {
        "ts": ts,
        "provider": "openai",
        "model": completion.model,
        "status": "success",
        "endpoint": "chat.completions",
        "input_tokens": getattr(usage, "prompt_tokens", None) if usage else None,
        "output_tokens": getattr(usage, "completion_tokens", None) if usage else None,
        "error": content_fetch_error is not None,
        "error_type": (
            type(content_fetch_error).__name__ if content_fetch_error else None
        ),
        "request_id": completion.id,
        "tags": tags,
        "route": route,
        "content_opted_in": content_opted_in,
        "request_json": request_json,
        "response_text": response_text,
        "sdk": "metergraphrelay",
        "sdk_version": __version__,
    }
    return with_import_provenance(
        row,
        source="openai",
        scope=source_scope,
        event_id=completion.id,
        source_trace_id=completion.id,
    )


def pull_openai(
    client: Any,
    count: int,
    output_path: str,
    *,
    route: str,
    include_content: bool,
    echo_stdout: bool = False,
    source_scope: str = "default",
) -> int:
    page = client.chat.completions.list(order="desc", limit=count)
    completions = list(islice(iter(page), count))
    written = 0
    with open(output_path, "w") as f:
        for completion in completions:
            if not include_content:
                row = normalize_completion(
                    completion,
                    [],
                    route=route,
                    include_content=include_content,
                    source_scope=source_scope,
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
                        completion,
                        [],
                        route=route,
                        include_content=include_content,
                        content_fetch_error=exc,
                        source_scope=source_scope,
                    )
                else:
                    row = normalize_completion(
                        completion,
                        messages,
                        route=route,
                        include_content=include_content,
                        source_scope=source_scope,
                    )
            line = json.dumps(row)
            f.write(line + "\n")
            if echo_stdout:
                print(line)
            written += 1
    return written
