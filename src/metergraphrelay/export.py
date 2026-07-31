from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from itertools import islice
from typing import Any, Iterable


def normalize_completion(
    completion: Any, messages: Iterable[Any], error: Exception | None = None
) -> dict:
    usage = getattr(completion, "usage", None)
    metadata = getattr(completion, "metadata", None) or {}
    ts = datetime.fromtimestamp(completion.created, tz=timezone.utc).isoformat()

    if error is not None:
        return {
            "id": completion.id,
            "ts": ts,
            "model": completion.model,
            "provider": "openai",
            "endpoint": "chat.completions",
            "status": "error",
            "input_tokens": None,
            "output_tokens": None,
            "messages": [],
            "metadata": metadata,
        }

    return {
        "id": completion.id,
        "ts": ts,
        "model": completion.model,
        "provider": "openai",
        "endpoint": "chat.completions",
        "status": "success",
        "input_tokens": getattr(usage, "prompt_tokens", None) if usage else None,
        "output_tokens": getattr(usage, "completion_tokens", None) if usage else None,
        "messages": [
            {"role": message.role, "content": message.content} for message in messages
        ],
        "metadata": metadata,
    }


def export_traces(
    client: Any, count: int, output_path: str, *, echo_stdout: bool = False
) -> int:
    page = client.chat.completions.list(order="desc", limit=count)
    completions = list(islice(iter(page), count))
    written = 0
    with open(output_path, "w") as f:
        for completion in completions:
            try:
                messages = client.chat.completions.messages.list(completion.id)
            except Exception as exc:
                print(
                    f"Warning: could not fetch messages for {completion.id}: {exc}",
                    file=sys.stderr,
                )
                row = normalize_completion(completion, [], error=exc)
            else:
                row = normalize_completion(completion, messages)
            line = json.dumps(row)
            f.write(line + "\n")
            if echo_stdout:
                print(line)
            written += 1
    return written
