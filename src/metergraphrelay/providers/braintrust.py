from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from typing import Any

from .. import __version__
from ..window import normalize_utc_designator

# Braintrust's US data plane. The EU plane (https://api-eu.braintrust.dev) and a
# self-hosted deployment's universal API URL are reachable via --base-url /
# $BRAINTRUST_BASE_URL.
DEFAULT_BRAINTRUST_URL = "https://api.braintrust.dev"
BTQL_PATH = "/btql"
PAGE_LIMIT = 1000
# span_attributes.type is Braintrust's own span classifier ("llm", "score",
# "function", "eval", "task", "tool", "review"). Only "llm" spans are model
# calls; every other type is application/eval structure and is never imported.
LLM_SPAN_TYPE = "llm"
# The cursor is returned as a response *header*, not in the body. Braintrust's
# own pagination example reads the S3-metadata spelling as a fallback, so both
# are checked here.
CURSOR_HEADERS = ("x-bt-cursor", "x-amz-meta-bt_cursor")
# `input`, `output`, `expected`, `error` and `metadata` are "preview" fields the
# query engine may truncate. -1 disables truncation; without it an import could
# silently upload clipped prompts/responses to metergraph.
PREVIEW_LENGTH_UNTRUNCATED = -1
# Braintrust fails a query server-side at 30s. A longer client timeout means a
# slow query surfaces as Braintrust's own error rather than a local timeout.
REQUEST_TIMEOUT_SECONDS = 60.0

# Exactly the columns normalize_span consumes. `estimated_cost()` is the
# documented way to read cost: it returns metrics.estimated_cost when the span
# logged one and otherwise derives it from token metrics and model-registry
# pricing, so it stays correct on spans that never logged a cost.
# `scores` is deliberately absent — Braintrust scores/evals are never imported,
# matching `pull langfuse`.
SELECT_FIELDS = (
    "id",
    "created",
    "span_id",
    "root_span_id",
    "span_parents",
    "span_attributes",
    "input",
    "output",
    "error",
    "metadata",
    "metrics",
    "tags",
    "project_id",
    "estimated_cost() AS estimated_cost",
)


class BraintrustAPIError(Exception):
    """Raised when Braintrust's /btql API errors or returns an unusable body."""


def _sql_string(value: str) -> str:
    """Quote ``value`` as a BTQL/SQL string literal.

    Single quotes are doubled, the standard SQL escape. A backslash or NUL is
    rejected outright rather than escaped: Braintrust does not document whether
    its lexer treats a backslash as an escape character (its `LIKE` examples
    pass one through to the pattern), so a trailing backslash could otherwise
    escape the closing quote. Project names and ids never legitimately contain
    either character, so rejecting is strictly safer than guessing.
    """
    if not isinstance(value, str) or not value:
        raise BraintrustAPIError(
            f"expected a non-empty string for a query literal, got {value!r}"
        )
    if "\\" in value or "\x00" in value:
        raise BraintrustAPIError(
            f"query literal may not contain a backslash or NUL byte: {value!r}"
        )
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def build_query(
    *,
    projects: list[str],
    since: str | None,
    until: str,
    limit: int,
    cursor: str | None = None,
) -> str:
    """Build the BTQL/SQL query for one page of LLM spans.

    `project_logs()` resolves each argument as either a project id or a project
    name, so callers pass whichever they have. `shape => 'spans'` returns the
    individual matching spans; the default `traces` shape would return every
    sibling span of any trace containing an LLM call, which is not what a
    per-call import wants.

    ``ORDER BY _pagination_key DESC`` is required for cursor pagination —
    Braintrust only issues a cursor for cursor-compatible sorts.
    """
    if not projects:
        raise BraintrustAPIError("at least one project is required")
    sources = ", ".join(_sql_string(project) for project in projects)
    clauses = [
        f"SELECT {', '.join(SELECT_FIELDS)}",
        f"FROM project_logs({sources}, shape => 'spans')",
        f"WHERE span_attributes.type = {_sql_string(LLM_SPAN_TYPE)}",
        f"  AND created < {_sql_string(until)}",
    ]
    if since:
        clauses.append(f"  AND created >= {_sql_string(since)}")
    clauses.append("ORDER BY _pagination_key DESC")
    clauses.append(f"LIMIT {int(limit)}")
    if cursor:
        # SQL syntax carries the cursor as OFFSET '<token>'; numeric offsets are
        # not supported.
        clauses.append(f"OFFSET {_sql_string(cursor)}")
    clauses.append(f"SETTINGS preview_length = {PREVIEW_LENGTH_UNTRUNCATED}")
    return "\n".join(clauses)


def _read_cursor(headers: Any) -> str | None:
    for name in CURSOR_HEADERS:
        value = headers.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def fetch_spans_page(
    base_url: str, *, api_key: str, query: str
) -> tuple[list[Any], str | None]:
    """POST one query to /btql, returning (rows, next_cursor)."""
    url = f"{base_url.rstrip('/')}{BTQL_PATH}"
    body = json.dumps({"query": query, "fmt": "json"}).encode()
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request, timeout=REQUEST_TIMEOUT_SECONDS
        ) as response:
            raw = response.read()
            cursor = _read_cursor(response.headers)
    except urllib.error.HTTPError as exc:
        raise BraintrustAPIError(
            f"Braintrust API request failed: HTTP {exc.code} {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise BraintrustAPIError(
            f"Braintrust API request failed: {exc.reason}"
        ) from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BraintrustAPIError(
            f"Braintrust API returned invalid JSON: {exc}"
        ) from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise BraintrustAPIError(
            "Braintrust API response missing/malformed 'data' — the /btql endpoint "
            "returns {\"data\": [...]} for fmt=json; check the base URL points at a "
            "Braintrust data plane"
        )
    return data, cursor


# Illustrative, not exhaustive: only consulted when the span itself carries no
# metadata.provider, which the Braintrust SDK integrations normally do set.
_PROVIDER_MODEL_PREFIXES: tuple[tuple[str, str], ...] = (
    ("gpt-", "openai"),
    ("o1-", "openai"),
    ("o3-", "openai"),
    ("chatgpt-", "openai"),
    ("claude-", "anthropic"),
    ("gemini-", "google"),
)


def _metadata(span: dict[str, Any]) -> dict[str, Any]:
    metadata = span.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _resolve_model_name(span: dict[str, Any]) -> str | None:
    value = _metadata(span).get("model")
    return value if isinstance(value, str) and value else None


def infer_provider(span: dict[str, Any]) -> str:
    explicit = _metadata(span).get("provider")
    if isinstance(explicit, str):
        explicit = explicit.strip().lower()
        if explicit:
            return explicit
    model_name = (_resolve_model_name(span) or "").lower()
    for prefix, provider in _PROVIDER_MODEL_PREFIXES:
        if model_name.startswith(prefix):
            return provider
    return "unknown"


def _is_chat_message_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(
        isinstance(item, dict) and "role" in item and "content" in item
        for item in value
    )


_JSON_PARSE_FAILED = object()


def _try_parse_json(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return _JSON_PARSE_FAILED


def _map_content(input_value: Any) -> tuple[str | None, str | None]:
    """Split a span's ``input`` into (request_json, request_text).

    A chat-message list is the structured request; anything else is carried as
    text so nothing is dropped.
    """
    if input_value is None:
        return None, None
    if _is_chat_message_list(input_value):
        return json.dumps(input_value), None
    if isinstance(input_value, str):
        parsed = _try_parse_json(input_value)
        if _is_chat_message_list(parsed):
            return json.dumps(parsed), None
        return None, input_value
    return None, json.dumps(input_value)


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


def _content_blocks(blocks: list) -> tuple[str | None, list | None]:
    """Split an Anthropic-style content-block list into (text, tool_calls)."""
    text_parts: list[str] = []
    tool_calls: list[Any] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            text_parts.append(block["text"])
        elif block.get("type") == "tool_use":
            tool_calls.append(block)
    return ("\n".join(text_parts) or None), (tool_calls or None)


def _message_output(message: dict[str, Any]) -> tuple[str | None, list | None]:
    raw_tool_calls = message.get("tool_calls")
    tool_calls = (
        raw_tool_calls if isinstance(raw_tool_calls, list) and raw_tool_calls else None
    )
    content = message.get("content")
    if isinstance(content, str):
        return (content or None), tool_calls
    if isinstance(content, list):
        block_text, block_tool_calls = _content_blocks(content)
        if block_text is not None or block_tool_calls is not None:
            return block_text, (tool_calls or block_tool_calls)
        # Every block was of a kind this doesn't recognize (e.g. "thinking").
        # Fall through to serializing the message rather than dropping it.
    if content is None and tool_calls:
        # A pure tool-call turn has no text content — that is not a failure to
        # extract, so don't fall back to dumping the whole message as text.
        return None, tool_calls
    return json.dumps(message), tool_calls


def _extract_output(output_value: Any) -> tuple[str | None, list | None]:
    """Map a span's ``output`` onto (response_text, tool_calls).

    Braintrust stores whatever the integration logged. In practice that is a
    single assistant message object, a content-block list, or a plain string;
    anything else is serialized whole into response_text rather than dropped
    (metergraph's row has no response_json counterpart to request_json).
    """
    if output_value is None:
        return None, None
    if isinstance(output_value, str):
        return (output_value or None), None
    if isinstance(output_value, dict):
        return _message_output(output_value)
    if isinstance(output_value, list):
        if output_value and all(isinstance(item, dict) for item in output_value):
            if any("type" in item for item in output_value):
                text, tool_calls = _content_blocks(output_value)
                if text is not None or tool_calls is not None:
                    return text, tool_calls
            elif len(output_value) == 1:
                return _message_output(output_value[0])
    return json.dumps(output_value), None


# Braintrust normalizes token usage onto its own metric names, and its stated
# convention already matches metergraph's: prompt_tokens is the TOTAL and
# includes both prompt_cached_tokens (cache reads) and prompt_cache_creation_
# tokens (cache writes). So — unlike Langfuse's flattened shape — nothing is
# added back here. The extra keys below are provider-native spellings that show
# up on spans imported through OpenTelemetry or logged by hand.
_INPUT_TOTAL_KEYS = ("prompt_tokens", "input_tokens", "promptTokenCount")
_OUTPUT_TOTAL_KEYS = ("completion_tokens", "output_tokens", "candidatesTokenCount")
_CACHE_READ_KEYS = (
    "prompt_cached_tokens",
    "cached_tokens",
    "cache_read_input_tokens",
)
_CACHE_WRITE_KEYS = (
    "prompt_cache_creation_tokens",
    "cache_creation_input_tokens",
)
# Anthropic instrumentation reports cache writes split by TTL *in place of* the
# aggregate, so summing these is the only way to see cache writes on those spans.
_CACHE_WRITE_TTL_KEYS = (
    "prompt_cache_creation_5m_tokens",
    "prompt_cache_creation_1h_tokens",
)
_REASONING_KEYS = (
    "completion_reasoning_tokens",
    "reasoning_tokens",
    "reasoning_output_tokens",
)


def _first_int(source: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _sum_ints(source: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    values = [
        value
        for key in keys
        if isinstance(value := source.get(key), int) and not isinstance(value, bool)
    ]
    return sum(values) if values else None


def map_metrics(metrics: Any) -> dict[str, int | None]:
    """Map one span's ``metrics`` onto metergraph token and latency fields."""
    empty: dict[str, int | None] = {
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "reasoning_tokens": None,
        "latency_ms": None,
    }
    if not isinstance(metrics, dict):
        return empty

    cache_write = _first_int(metrics, _CACHE_WRITE_KEYS)
    if cache_write is None:
        cache_write = _sum_ints(metrics, _CACHE_WRITE_TTL_KEYS)

    return {
        "input_tokens": _first_int(metrics, _INPUT_TOTAL_KEYS),
        "output_tokens": _first_int(metrics, _OUTPUT_TOTAL_KEYS),
        "cache_read_tokens": _first_int(metrics, _CACHE_READ_KEYS),
        "cache_write_tokens": cache_write,
        "reasoning_tokens": _first_int(metrics, _REASONING_KEYS),
        "latency_ms": _latency_ms(metrics),
    }


def _latency_ms(metrics: dict[str, Any]) -> int | None:
    """Derive latency from the span's epoch-seconds start/end metrics."""
    start = metrics.get("start")
    end = metrics.get("end")
    if (
        isinstance(start, bool)
        or isinstance(end, bool)
        or not isinstance(start, (int, float))
        or not isinstance(end, (int, float))
        or end < start
    ):
        return None
    return round((end - start) * 1000)


def _error_type(raw_error: Any) -> str | None:
    if isinstance(raw_error, str):
        return raw_error
    if isinstance(raw_error, dict):
        message = raw_error.get("message")
        return message if isinstance(message, str) else json.dumps(raw_error)
    return json.dumps(raw_error)


def _is_error(raw_error: Any) -> bool:
    # A span with no error logs `error: null`. An empty/whitespace string is
    # treated the same way — it carries no failure information.
    if raw_error is None:
        return False
    if isinstance(raw_error, str):
        return bool(raw_error.strip())
    return True


def _cost_usd(span: dict[str, Any], metrics: Any) -> float | int | None:
    # `estimated_cost() AS estimated_cost` lands as a top-level column; fall
    # back to the raw metric for a caller that selected columns differently.
    for source in (span, metrics if isinstance(metrics, dict) else {}):
        value = source.get("estimated_cost")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
    return None


def normalize_span(span: dict[str, Any], *, route_override: str | None) -> dict:
    span_attributes = span.get("span_attributes")
    span_attributes = span_attributes if isinstance(span_attributes, dict) else {}
    raw_name = span_attributes.get("name")
    name = raw_name if isinstance(raw_name, str) and raw_name else None

    if route_override:
        route = route_override
        name_consumed = False
    else:
        # The LLM span's own name (e.g. "OpenAI Chat Completion") is the only
        # per-call label present on a spans-shaped row: a Braintrust trace has
        # no name field of its own, and the root span that would carry a
        # workflow name is a different row this query never returns.
        route = name or "braintrust/backfill"
        name_consumed = name is not None

    tags: dict[str, Any] = {}
    span_tags = span.get("tags")
    if isinstance(span_tags, list) and span_tags:
        tags["braintrust_tags"] = list(span_tags)
    project_id = span.get("project_id")
    if isinstance(project_id, str) and project_id:
        tags["braintrust_project_id"] = project_id
    if not name_consumed and name:
        tags["name"] = name

    raw_error = span.get("error")
    error = _is_error(raw_error)

    metrics = span.get("metrics")
    usage = map_metrics(metrics)
    request_json, request_text = _map_content(span.get("input"))
    response_text, tool_calls = _extract_output(span.get("output"))

    span_parents = span.get("span_parents")
    parent_span_id = (
        span_parents[0]
        if isinstance(span_parents, list) and span_parents
        else None
    )

    return {
        # Braintrust's documented `created` carries an explicit `+00:00` offset,
        # but a `Z` designator would be parsed by `datetime.fromisoformat` only
        # on Python 3.11+; a 3.10 consumer (the OSS server supports 3.10) fails
        # the parse and silently substitutes ingest time. Normalizing the
        # designator here keeps a historical timestamp historical everywhere.
        # A non-string `created` raises AttributeError, which the caller's skip
        # path reports rather than forwarding an unusable timestamp.
        "ts": normalize_utc_designator(span["created"]),
        "source": "braintrust",
        "sdk": "metergraphrelay",
        "sdk_version": __version__,
        "provider": infer_provider(span),
        "model": _resolve_model_name(span),
        "status": "error" if error else "success",
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "cache_read_tokens": usage["cache_read_tokens"],
        "cache_write_tokens": usage["cache_write_tokens"],
        "reasoning_tokens": usage["reasoning_tokens"],
        "latency_ms": usage["latency_ms"],
        "cost_usd": _cost_usd(span, metrics),
        "error": error,
        "error_type": _error_type(raw_error) if error else None,
        "request_id": span["id"],
        "tags": tags,
        "route": route,
        "content_opted_in": True,
        "request_json": request_json,
        "request_text": request_text,
        "response_text": response_text,
        "tool_calls": tool_calls,
        "tool_names": _tool_names(tool_calls),
        "trace_id": span.get("root_span_id"),
        "span_id": span.get("span_id") or span["id"],
        "parent_span_id": parent_span_id,
    }


def _cleanup_temp_file(tmp_path: str) -> None:
    try:
        os.remove(tmp_path)
    except OSError:
        pass


def pull_braintrust(
    *,
    base_url: str,
    api_key: str,
    projects: list[str],
    count: int,
    since: str | None,
    until: str,
    route: str | None,
    output_path: str,
) -> tuple[int, int]:
    imported = 0
    skipped = 0
    cursor: str | None = None
    used_cursors: set[str] = set()
    # The cursor is bound to the query that produced it, so every page must ask
    # for the same LIMIT — only the OFFSET clause changes between pages. The cap
    # on `count` is enforced while writing rows instead.
    page_limit = max(1, min(PAGE_LIMIT, count))

    output_dir = os.path.dirname(output_path) or "."
    fd, tmp_path = tempfile.mkstemp(
        dir=output_dir, prefix=f".{os.path.basename(output_path)}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            while imported < count:
                if cursor is not None:
                    if cursor in used_cursors:
                        raise BraintrustAPIError(
                            "Braintrust API returned a repeated pagination cursor "
                            "(non-advancing pagination), aborting to avoid an "
                            f"infinite loop: {cursor!r}"
                        )
                    used_cursors.add(cursor)
                query = build_query(
                    projects=projects,
                    since=since,
                    until=until,
                    limit=page_limit,
                    cursor=cursor,
                )
                spans, cursor = fetch_spans_page(
                    base_url, api_key=api_key, query=query
                )
                if not spans:
                    break
                for span in spans:
                    if imported >= count:
                        break
                    try:
                        row = normalize_span(span, route_override=route)
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
        # On success os.replace has already moved tmp_path, so this is a no-op
        # (_cleanup_temp_file ignores a missing path); on any failure it removes
        # the still-present temp file before the exception propagates.
        _cleanup_temp_file(tmp_path)
    return imported, skipped
