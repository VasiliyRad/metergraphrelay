"""Pull Arize Phoenix LLM spans into metergraph-native JSONL rows.

Phoenix stores OpenTelemetry spans carrying OpenInference attributes, and
serves them through ``GET /v1/projects/{project}/spans`` with cursor
pagination and a ``span_kind`` filter. Only ``LLM`` spans are imported:
CHAIN/TOOL/RETRIEVER/AGENT spans are application structure, not model calls,
and Phoenix annotations and evals are never read.

Phoenix has no notion of a workflow name on the span itself, so the route is
resolved from the most specific source available:

1. ``metergraph.route`` — set by a caller that instrumented for metergraph.
2. ``gen_ai.operation.name`` — the OpenTelemetry GenAI convention, which the
   metergraph SDK's span tee also reads.
3. the span's own name — an OpenInference instrumentor names its LLM span
   after the SDK method (``ChatCompletion``), so live auto-instrumented
   traffic lands on that name unless ``--route`` is passed.
4. ``phoenix/backfill``.

Token counts come from the ``llm.token_count.*`` attributes. OpenInference's
``prompt`` is the total prompt count with ``prompt_details.cache_read`` as a
subset of it, which matches metergraph's convention, so nothing is added back.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

from .. import __version__
from ..window import normalize_utc_designator

DEFAULT_PHOENIX_URL = "http://localhost:6006"
SPANS_PATH_TEMPLATE = "/v1/projects/{project}/spans"
PAGE_LIMIT = 1000
LLM_SPAN_KIND = "LLM"
REQUEST_TIMEOUT_SECONDS = 30.0
BACKFILL_ROUTE = "phoenix/backfill"

ATTR_MODEL = "llm.model_name"
ATTR_PROVIDER = "llm.provider"
ATTR_SYSTEM = "llm.system"
ATTR_INPUT_TOKENS = "llm.token_count.prompt"
ATTR_OUTPUT_TOKENS = "llm.token_count.completion"
ATTR_CACHE_READ_TOKENS = "llm.token_count.prompt_details.cache_read"
ATTR_CACHE_WRITE_TOKENS = "llm.token_count.prompt_details.cache_write"
ATTR_REASONING_TOKENS = "llm.token_count.completion_details.reasoning"
ATTR_INPUT_VALUE = "input.value"
ATTR_INPUT_MIME = "input.mime_type"
ATTR_OUTPUT_VALUE = "output.value"
ATTR_INPUT_MESSAGES = "llm.input_messages"
ATTR_OUTPUT_MESSAGES = "llm.output_messages"
ROUTE_ATTRIBUTES = ("metergraph.route", "gen_ai.operation.name")


class PhoenixAPIError(Exception):
    """Raised when Phoenix's API returns an error response or an unusable body."""


def build_params(
    *,
    since: str | None,
    until: str | None,
    names: list[str],
    limit: int,
    cursor: str | None = None,
) -> list[tuple[str, str]]:
    """Query parameters for one page. A list, because ``name`` repeats."""
    params: list[tuple[str, str]] = [
        ("span_kind", LLM_SPAN_KIND),
        ("limit", str(int(limit))),
    ]
    if since:
        params.append(("start_time", since))
    if until:
        params.append(("end_time", until))
    for name in names:
        params.append(("name", name))
    if cursor:
        params.append(("cursor", cursor))
    return params


def fetch_spans_page(
    base_url: str,
    *,
    project: str,
    api_key: str | None,
    params: list[tuple[str, str]],
) -> tuple[list[Any], str | None]:
    """GET one page of spans for a project, returning (rows, next_cursor)."""
    path = SPANS_PATH_TEMPLATE.format(project=urllib.parse.quote(project, safe=""))
    url = f"{base_url.rstrip('/')}{path}"
    query = urllib.parse.urlencode(params)
    if query:
        url = f"{url}?{query}"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        if exc.code == 404:
            detail = f" (is {project!r} a Phoenix project name or id?)"
        raise PhoenixAPIError(
            f"Phoenix API request failed: HTTP {exc.code} {exc.reason}{detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise PhoenixAPIError(f"Phoenix API request failed: {exc.reason}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PhoenixAPIError(f"Phoenix API returned invalid JSON: {exc}") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise PhoenixAPIError(
            "Phoenix API response missing/malformed 'data' — the spans endpoint "
            "returns {\"data\": [...], \"next_cursor\": ...}; check the base URL "
            "points at a Phoenix server (13.15 or newer)"
        )
    raw_cursor = payload.get("next_cursor")
    if raw_cursor is not None and not (isinstance(raw_cursor, str) and raw_cursor):
        raise PhoenixAPIError(
            f"Phoenix API returned a malformed pagination cursor: {raw_cursor!r}"
        )
    return data, raw_cursor


# Illustrative, not exhaustive: only consulted when the span carries neither
# llm.provider nor llm.system, which OpenInference instrumentors normally set.
_PROVIDER_MODEL_PREFIXES: tuple[tuple[str, str], ...] = (
    ("gpt-", "openai"),
    ("o1-", "openai"),
    ("o3-", "openai"),
    ("chatgpt-", "openai"),
    ("claude-", "anthropic"),
    ("gemini-", "google"),
)


def _attributes(span: dict[str, Any]) -> dict[str, Any]:
    attributes = span.get("attributes")
    return attributes if isinstance(attributes, dict) else {}


def _str(attributes: dict[str, Any], key: str) -> str | None:
    value = attributes.get(key)
    return value if isinstance(value, str) and value else None


def _int(attributes: dict[str, Any], key: str) -> int | None:
    value = attributes.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _resolve_model_name(span: dict[str, Any]) -> str | None:
    return _str(_attributes(span), ATTR_MODEL)


def infer_provider(span: dict[str, Any]) -> str:
    attributes = _attributes(span)
    for key in (ATTR_PROVIDER, ATTR_SYSTEM):
        explicit = _str(attributes, key)
        if explicit:
            return explicit.strip().lower()
    model_name = (_resolve_model_name(span) or "").lower()
    for prefix, provider in _PROVIDER_MODEL_PREFIXES:
        if model_name.startswith(prefix):
            return provider
    return "unknown"


def _messages(attributes: dict[str, Any], prefix: str) -> list[dict[str, Any]]:
    """Reassemble ``<prefix>.<i>.message.*`` flattened attributes into a list.

    Indices are read in order until one is missing, so a gap ends the list.
    """
    messages: list[dict[str, Any]] = []
    index = 0
    while True:
        base = f"{prefix}.{index}.message."
        keys = [key for key in attributes if key.startswith(base)]
        if not keys:
            break
        message: dict[str, Any] = {}
        for key in keys:
            field = key[len(base):]
            if "." in field:
                # Nested tool call / content block attributes are kept out of
                # the plain chat message list; tool names are read separately
                # and content blocks are folded in below.
                continue
            message[field] = attributes[key]
        message.setdefault("role", "")
        if not message.get("content"):
            # Multimodal and content-block messages carry their text under
            # contents.<j>.message_content.text instead of a plain content.
            message["content"] = _content_block_text(attributes, base)
        messages.append(message)
        index += 1
    return messages


def _content_block_text(attributes: dict[str, Any], base: str) -> str:
    texts: list[str] = []
    index = 0
    while True:
        block = f"{base}contents.{index}.message_content."
        if not any(key.startswith(block) for key in attributes):
            break
        text = attributes.get(f"{block}text")
        if isinstance(text, str) and text:
            texts.append(text)
        index += 1
    return "\n".join(texts)


def _tool_names(attributes: dict[str, Any]) -> list[str] | None:
    """Tool call names across every output message, in order."""
    names: list[str] = []
    message = 0
    while True:
        base = f"{ATTR_OUTPUT_MESSAGES}.{message}.message."
        if not any(key.startswith(base) for key in attributes):
            break
        call = 0
        while True:
            name = _str(attributes, f"{base}tool_calls.{call}.tool_call.function.name")
            if name is None:
                break
            names.append(name)
            call += 1
        message += 1
    return names or None


def _is_chat_message_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(
        isinstance(item, dict) and "role" in item and "content" in item
        for item in value
    )


def _map_input(attributes: dict[str, Any]) -> tuple[str | None, str | None]:
    """(request_json, request_text) from the flattened messages or input.value.

    Flattened messages win when any of them carries content. A message list
    whose content is entirely empty (an instrumentor that only emitted nested
    attributes this reader does not understand) falls through to input.value,
    which holds the whole request payload, rather than shipping blank turns.
    """
    messages = _messages(attributes, ATTR_INPUT_MESSAGES)
    if messages and any(message.get("content") for message in messages):
        return json.dumps(messages), None
    raw = attributes.get(ATTR_INPUT_VALUE)
    if raw is None:
        return (json.dumps(messages), None) if messages else (None, None)
    if isinstance(raw, str):
        if _str(attributes, ATTR_INPUT_MIME) == "application/json":
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return None, raw
            candidate = parsed.get("messages") if isinstance(parsed, dict) else parsed
            if _is_chat_message_list(candidate):
                return json.dumps(candidate), None
        return None, raw
    return None, json.dumps(raw)


def _response_text(attributes: dict[str, Any]) -> str | None:
    messages = _messages(attributes, ATTR_OUTPUT_MESSAGES)
    texts = [
        message["content"]
        for message in messages
        if isinstance(message.get("content"), str) and message["content"]
    ]
    if texts:
        return "\n".join(texts)
    raw = attributes.get(ATTR_OUTPUT_VALUE)
    if raw is None:
        return None
    return raw if isinstance(raw, str) else json.dumps(raw)


def _latency_ms(start_time: Any, end_time: Any) -> int | None:
    if not isinstance(start_time, str) or not isinstance(end_time, str):
        return None
    try:
        start = datetime.fromisoformat(normalize_utc_designator(start_time))
        end = datetime.fromisoformat(normalize_utc_designator(end_time))
        if end < start:
            return None
    except (ValueError, TypeError):
        # Unparseable, or a naive/aware pair that cannot be compared: the span
        # still imports, only without latency.
        return None
    return round((end - start).total_seconds() * 1000)


def _resolve_route(
    attributes: dict[str, Any], name: str | None, route_override: str | None
) -> tuple[str, bool]:
    """(route, name_consumed): whether the span's own name became the route."""
    if route_override:
        return route_override, False
    for key in ROUTE_ATTRIBUTES:
        value = _str(attributes, key)
        if value:
            return value, False
    if name:
        return name, True
    return BACKFILL_ROUTE, False


def normalize_span(
    span: dict[str, Any], *, project: str, route_override: str | None
) -> dict:
    attributes = _attributes(span)
    raw_name = span.get("name")
    name = raw_name if isinstance(raw_name, str) and raw_name else None
    route, name_consumed = _resolve_route(attributes, name, route_override)

    tags: dict[str, Any] = {"phoenix_project": project}
    if not name_consumed and name:
        tags["name"] = name

    error = span.get("status_code") == "ERROR"
    status_message = span.get("status_message")
    error_type = (
        status_message
        if error and isinstance(status_message, str) and status_message
        else None
    )

    request_json, request_text = _map_input(attributes)
    context = span.get("context")
    context = context if isinstance(context, dict) else {}
    span_id = context.get("span_id") or span["id"]

    return {
        "ts": normalize_utc_designator(span["start_time"]),
        "source": "phoenix",
        "sdk": "metergraphrelay",
        "sdk_version": __version__,
        "provider": infer_provider(span),
        "model": _resolve_model_name(span),
        "status": "error" if error else "success",
        "input_tokens": _int(attributes, ATTR_INPUT_TOKENS),
        "output_tokens": _int(attributes, ATTR_OUTPUT_TOKENS),
        "cache_read_tokens": _int(attributes, ATTR_CACHE_READ_TOKENS),
        "cache_write_tokens": _int(attributes, ATTR_CACHE_WRITE_TOKENS),
        "reasoning_tokens": _int(attributes, ATTR_REASONING_TOKENS),
        "latency_ms": _latency_ms(span.get("start_time"), span.get("end_time")),
        # Phoenix prices spans in its UI but the spans endpoint does not
        # return the figure; metergraph's own catalog prices the row instead.
        "cost_usd": None,
        "error": error,
        "error_type": error_type,
        "request_id": span_id,
        "tags": tags,
        "route": route,
        "content_opted_in": True,
        "request_json": request_json,
        "request_text": request_text,
        "response_text": _response_text(attributes),
        "tool_names": _tool_names(attributes),
        "trace_id": context.get("trace_id"),
        "span_id": span_id,
        "parent_span_id": span.get("parent_id"),
    }


def _cleanup_temp_file(tmp_path: str) -> None:
    try:
        os.remove(tmp_path)
    except OSError:
        pass


def pull_phoenix(
    *,
    base_url: str,
    api_key: str | None,
    projects: list[str],
    count: int,
    since: str | None,
    until: str | None,
    names: list[str],
    route: str | None,
    output_path: str,
) -> tuple[int, int]:
    """Pull up to ``count`` LLM spans across ``projects`` into ``output_path``.

    Projects are read in the order given; the count cap spans all of them.
    """
    if not projects:
        raise PhoenixAPIError("at least one project is required")
    imported = 0
    skipped = 0
    filtered = 0

    output_dir = os.path.dirname(output_path) or "."
    fd, tmp_path = tempfile.mkstemp(
        dir=output_dir, prefix=f".{os.path.basename(output_path)}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            for project in projects:
                cursor: str | None = None
                used_cursors: set[str] = set()
                while imported < count:
                    if cursor:
                        if cursor in used_cursors:
                            raise PhoenixAPIError(
                                "Phoenix API returned a repeated pagination cursor "
                                "(non-advancing pagination), aborting to avoid an "
                                f"infinite loop: {cursor!r}"
                            )
                        used_cursors.add(cursor)
                    params = build_params(
                        since=since,
                        until=until,
                        names=names,
                        limit=min(PAGE_LIMIT, count - imported),
                        cursor=cursor,
                    )
                    spans, cursor = fetch_spans_page(
                        base_url, project=project, api_key=api_key, params=params
                    )
                    if not spans:
                        break
                    for span in spans:
                        if imported >= count:
                            break
                        if isinstance(span, dict) and span.get("span_kind") not in (None, LLM_SPAN_KIND):
                            # Phoenix before 13.15 ignores the span_kind query
                            # parameter and returns every kind; never let a
                            # CHAIN/TOOL/RETRIEVER span import as a model call.
                            filtered += 1
                            continue
                        try:
                            row = normalize_span(
                                span, project=project, route_override=route
                            )
                            line = json.dumps(row)
                        except (KeyError, TypeError, AttributeError) as exc:
                            skipped += 1
                            span_id = (
                                span.get("id", "<unknown>")
                                if isinstance(span, dict)
                                else "<unknown>"
                            )
                            print(
                                f"Warning: skipping malformed span {span_id}: {exc}",
                                file=sys.stderr,
                            )
                            continue
                        f.write(line + "\n")
                        imported += 1
                    if not cursor:
                        break
        os.replace(tmp_path, output_path)
    finally:
        _cleanup_temp_file(tmp_path)
    if filtered:
        print(
            f"Warning: Phoenix returned {filtered} non-LLM span(s) despite the "
            "span_kind filter (servers before 13.15 ignore it); they were left out.",
            file=sys.stderr,
        )
    return imported, skipped
