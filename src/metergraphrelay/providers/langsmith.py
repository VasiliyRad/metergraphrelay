"""Pull LangSmith LLM runs into metergraph-native JSONL rows.

LangSmith records every traced call as a *run*; the model calls are the runs
with ``run_type = "llm"``. They are read through ``POST /runs/query``, which
takes project ids (``session``), a run type, a start-time lower bound, a
filter expression, and a cursor, and returns ``{"runs": [...], "cursors":
{"next": ...}}``. Projects may be given by name; a name is resolved to its id
through ``GET /sessions``.

Only LLM runs are imported. Chain, tool, retriever and prompt runs are
application structure, not model calls, and feedback is never read.

A LangSmith LLM run has no workflow name of its own: the trace's root run
carries one, but it is a different row this query never returns. The route is
therefore the run's own name (LangChain names it after the model class, e.g.
``ChatOpenAI``), falling back to ``langsmith/backfill``; ``--route`` sets it
for every imported row.

Token counts come from the run's ``prompt_tokens`` / ``completion_tokens``,
which LangSmith derives from LangChain's ``usage_metadata``: ``input_tokens``
there is the whole prompt with cache reads as a subset, the same convention
metergraph uses, so the totals are carried across unchanged. Cache and
reasoning buckets come from ``prompt_token_details`` /
``completion_token_details`` (``cache_read``, ``cache_creation``,
``reasoning``), with the raw ``usage_metadata`` in the run's metadata as the
fallback for runs LangSmith has not yet rolled up.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Callable

from .. import __version__
from ..import_identity import ImportContext, canonical_import_event_id
from ..window import normalize_utc_designator

DEFAULT_LANGSMITH_URL = "https://api.smith.langchain.com"
RUNS_QUERY_PATH = "/runs/query"
SESSIONS_PATH = "/sessions"
# /runs/query serves at most 100 rows per page.
PAGE_LIMIT = 100
LLM_RUN_TYPE = "llm"
REQUEST_TIMEOUT_SECONDS = 30.0
BACKFILL_ROUTE = "langsmith/backfill"
# Everything normalize_run reads. Requested explicitly so a server default
# that omits a field cannot silently null it.
SELECT_FIELDS = (
    "id", "name", "run_type", "start_time", "end_time", "error", "inputs",
    "outputs", "extra", "tags", "session_id", "trace_id", "parent_run_id",
    "prompt_tokens", "completion_tokens", "total_tokens",
    "prompt_token_details", "completion_token_details",
    "prompt_cost", "completion_cost", "total_cost",
)
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class LangSmithAPIError(Exception):
    """Raised for anything that should abort the pull with a user-facing message."""


def _filter_string(value: str) -> str:
    """Quote a value for LangSmith's filter DSL (double quotes, escaped)."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_filter(*, until: str | None, names: list[str], tags: list[str]) -> str | None:
    """Combine the selectors LangSmith's DSL has to carry.

    ``start_time`` has its own body field (a lower bound); the upper bound,
    run names (OR) and tags (AND) go through ``filter``.
    """
    clauses: list[str] = []
    if until:
        clauses.append(f"lt(start_time, {_filter_string(until)})")
    if names:
        name_clauses = [f"eq(name, {_filter_string(name)})" for name in names]
        clauses.append(name_clauses[0] if len(name_clauses) == 1 else f"or({', '.join(name_clauses)})")
    for tag in tags:
        clauses.append(f"has(tags, {_filter_string(tag)})")
    if not clauses:
        return None
    return clauses[0] if len(clauses) == 1 else f"and({', '.join(clauses)})"


def build_query(
    *,
    project_ids: list[str],
    since: str | None,
    until: str | None,
    names: list[str],
    tags: list[str],
    limit: int,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Body for one page of ``POST /runs/query``."""
    body: dict[str, Any] = {
        "session": list(project_ids),
        "run_type": LLM_RUN_TYPE,
        "select": list(SELECT_FIELDS),
        "limit": int(limit),
    }
    if since:
        body["start_time"] = since
    filter_expr = build_filter(until=until, names=names, tags=tags)
    if filter_expr:
        body["filter"] = filter_expr
    if cursor:
        body["cursor"] = cursor
    return body


def _request(
    base_url: str,
    *,
    api_key: str,
    method: str,
    path: str,
    query: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> Any:
    url = f"{base_url.rstrip('/')}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"x-api-key": api_key, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise LangSmithAPIError(
            f"LangSmith API request failed: HTTP {exc.code} {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise LangSmithAPIError(f"LangSmith API request failed: {exc.reason}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LangSmithAPIError(f"LangSmith API returned invalid JSON: {exc}") from exc


def resolve_project_ids(base_url: str, *, api_key: str, projects: list[str]) -> list[str]:
    """Turn project names or ids into the ids ``/runs/query`` wants."""
    ids: list[str] = []
    for project in projects:
        if _UUID_RE.match(project):
            ids.append(project)
            continue
        payload = _request(
            base_url, api_key=api_key, method="GET", path=SESSIONS_PATH,
            query={"name": project, "limit": "1"},
        )
        match = None
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict) and item.get("name") == project:
                    match = item
                    break
        if match is None or not isinstance(match.get("id"), str):
            raise LangSmithAPIError(
                f"LangSmith project {project!r} not found (is it a project name or id?)"
            )
        ids.append(match["id"])
    return ids


def fetch_runs_page(
    base_url: str, *, api_key: str, body: dict[str, Any]
) -> tuple[list[Any], str | None]:
    """POST one query, returning (runs, next_cursor)."""
    payload = _request(base_url, api_key=api_key, method="POST", path=RUNS_QUERY_PATH, body=body)
    runs = payload.get("runs") if isinstance(payload, dict) else None
    if not isinstance(runs, list):
        raise LangSmithAPIError(
            "LangSmith API response missing/malformed 'runs' — /runs/query returns "
            "{\"runs\": [...], \"cursors\": {...}}; check the base URL points at a "
            "LangSmith API host"
        )
    cursors = payload.get("cursors")
    raw_cursor = cursors.get("next") if isinstance(cursors, dict) else None
    if raw_cursor is not None and not (isinstance(raw_cursor, str) and raw_cursor):
        raise LangSmithAPIError(
            f"LangSmith API returned a malformed pagination cursor: {raw_cursor!r}"
        )
    return runs, raw_cursor


# Illustrative, not exhaustive: consulted only when the run carries no
# ls_provider, which LangChain's integrations normally set.
_PROVIDER_MODEL_PREFIXES: tuple[tuple[str, str], ...] = (
    ("gpt-", "openai"),
    ("o1-", "openai"),
    ("o3-", "openai"),
    ("chatgpt-", "openai"),
    ("claude-", "anthropic"),
    ("gemini-", "google"),
)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _int_or_none(value: Any) -> int | None:
    if _is_int(value):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _metadata(run: dict[str, Any]) -> dict[str, Any]:
    extra = run.get("extra")
    if not isinstance(extra, dict):
        return {}
    metadata = extra.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _invocation_params(run: dict[str, Any]) -> dict[str, Any]:
    extra = run.get("extra")
    if not isinstance(extra, dict):
        return {}
    params = extra.get("invocation_params")
    return params if isinstance(params, dict) else {}


def _resolve_model_name(run: dict[str, Any]) -> str | None:
    metadata = _metadata(run)
    for source, key in ((metadata, "ls_model_name"), (_invocation_params(run), "model"),
                        (_invocation_params(run), "model_name")):
        value = source.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def infer_provider(run: dict[str, Any]) -> str:
    explicit = _metadata(run).get("ls_provider")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip().lower()
    model_name = (_resolve_model_name(run) or "").lower()
    for prefix, provider in _PROVIDER_MODEL_PREFIXES:
        if model_name.startswith(prefix):
            return provider
    return "unknown"


def _usage_metadata(run: dict[str, Any]) -> dict[str, Any]:
    usage = _metadata(run).get("usage_metadata")
    return usage if isinstance(usage, dict) else {}


def _detail(run: dict[str, Any], rolled: str, raw_parent: str, key: str) -> int | None:
    """A detail bucket from the rolled-up field, else from raw usage_metadata."""
    details = run.get(rolled)
    if isinstance(details, dict):
        value = _int_or_none(details.get(key))
        if value is not None:
            return value
    raw = _usage_metadata(run).get(raw_parent)
    if isinstance(raw, dict):
        return _int_or_none(raw.get(key))
    return None


def map_usage(run: dict[str, Any]) -> dict[str, int | None]:
    """Token fields from the run's rolled-up counts, else raw usage_metadata."""
    usage = _usage_metadata(run)
    input_tokens = _int_or_none(run.get("prompt_tokens"))
    if input_tokens is None:
        input_tokens = _int_or_none(usage.get("input_tokens"))
    output_tokens = _int_or_none(run.get("completion_tokens"))
    if output_tokens is None:
        output_tokens = _int_or_none(usage.get("output_tokens"))
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": _detail(run, "prompt_token_details", "input_token_details", "cache_read"),
        "cache_write_tokens": _detail(run, "prompt_token_details", "input_token_details", "cache_creation"),
        "reasoning_tokens": _detail(run, "completion_token_details", "output_token_details", "reasoning"),
    }


def _cost_usd(run: dict[str, Any]) -> float | int | None:
    for source, key in ((run, "total_cost"), (_usage_metadata(run), "total_cost")):
        value = source.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
    return None


def _is_chat_message_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(
        isinstance(item, dict) and "role" in item and "content" in item
        for item in value
    )


def _flatten_langchain_messages(value: Any) -> list[dict[str, Any]] | None:
    """LangChain serializes chat messages as [[{"type"/"role", "content"}]] or
    {"lc", "kwargs": {"content"}} objects; reduce them to role/content pairs."""
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], list):
        value = value[0]
    if not isinstance(value, list) or not value:
        return None
    messages: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            return None
        if "kwargs" in item and isinstance(item["kwargs"], dict):
            kwargs = item["kwargs"]
            role = kwargs.get("type") or (item.get("id", [""])[-1] if isinstance(item.get("id"), list) else "")
            messages.append({"role": _lc_role(role), "content": kwargs.get("content", "")})
            continue
        role = item.get("role") or item.get("type") or ""
        if "content" not in item and "role" not in item and "type" not in item:
            return None
        messages.append({"role": _lc_role(role), "content": item.get("content", "")})
    return messages


def _lc_role(role: Any) -> str:
    if not isinstance(role, str):
        return ""
    return {"human": "user", "ai": "assistant", "HumanMessage": "user",
            "AIMessage": "assistant", "SystemMessage": "system"}.get(role, role)


def _map_input(inputs: Any) -> tuple[str | None, str | None]:
    """(request_json, request_text) from a run's inputs."""
    if inputs is None:
        return None, None
    if isinstance(inputs, dict):
        messages = inputs.get("messages")
        if _is_chat_message_list(messages):
            return json.dumps(messages), None
        flattened = _flatten_langchain_messages(messages)
        if flattened:
            return json.dumps(flattened), None
        prompt = inputs.get("prompt") or inputs.get("input")
        if isinstance(prompt, str):
            return None, prompt
        return None, json.dumps(inputs)
    return None, inputs if isinstance(inputs, str) else json.dumps(inputs)


def _response_text(outputs: Any) -> str | None:
    """Best text from a run's outputs, whatever shape recorded it."""
    if outputs is None:
        return None
    if isinstance(outputs, str):
        return outputs
    if not isinstance(outputs, dict):
        return json.dumps(outputs)
    # LangChain LLM result: {"generations": [[{"text": ...}]]}
    generations = outputs.get("generations")
    if isinstance(generations, list) and generations:
        first = generations[0]
        if isinstance(first, list) and first:
            first = first[0]
        if isinstance(first, dict):
            text = first.get("text")
            if isinstance(text, str):
                return text
            message = first.get("message")
            if isinstance(message, dict):
                kwargs = message.get("kwargs")
                if isinstance(kwargs, dict) and isinstance(kwargs.get("content"), str):
                    return kwargs["content"]
    # OpenAI-shaped: {"choices": [{"message": {"content": ...}}]}
    choices = outputs.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"]
    # A bare chat message, or {"output": ...}
    if isinstance(outputs.get("content"), str):
        return outputs["content"]
    output = outputs.get("output")
    if isinstance(output, str):
        return output
    if isinstance(output, dict) and isinstance(output.get("content"), str):
        return output["content"]
    return json.dumps(outputs)


def _latency_ms(start_time: Any, end_time: Any) -> int | None:
    if not isinstance(start_time, str) or not isinstance(end_time, str):
        return None
    try:
        start = datetime.fromisoformat(normalize_utc_designator(start_time))
        end = datetime.fromisoformat(normalize_utc_designator(end_time))
        if end < start:
            return None
    except (ValueError, TypeError):
        return None
    return round((end - start).total_seconds() * 1000)


def _error_type(raw_error: Any) -> str | None:
    if isinstance(raw_error, str):
        stripped = raw_error.strip()
        return stripped or None
    if raw_error is None:
        return None
    return json.dumps(raw_error)


def _timestamp(value: Any) -> str:
    """LangSmith returns naive ISO timestamps in UTC; make them explicit."""
    if not isinstance(value, str) or not value:
        raise KeyError("start_time")
    normalized = normalize_utc_designator(value)
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return normalized + "+00:00"
    return normalized


def normalize_run(
    run: dict[str, Any],
    *,
    route_override: str | None,
    import_context: ImportContext | None = None,
) -> dict:
    raw_name = run.get("name")
    name = raw_name if isinstance(raw_name, str) and raw_name else None
    if route_override:
        route, name_consumed = route_override, False
    else:
        route, name_consumed = (name or BACKFILL_ROUTE), name is not None

    tags: dict[str, Any] = {}
    run_tags = run.get("tags")
    if isinstance(run_tags, list) and run_tags:
        tags["langsmith_tags"] = list(run_tags)
    session_id = run.get("session_id")
    if isinstance(session_id, str) and session_id:
        tags["langsmith_project_id"] = session_id
    if not name_consumed and name:
        tags["name"] = name

    error_type = _error_type(run.get("error"))
    error = error_type is not None
    usage = map_usage(run)
    request_json, request_text = _map_input(run.get("inputs"))
    run_id = run["id"]

    row = {
        "ts": _timestamp(run.get("start_time")),
        "source": "langsmith",
        "sdk": "metergraphrelay",
        "sdk_version": __version__,
        "provider": infer_provider(run),
        "model": _resolve_model_name(run),
        "status": "error" if error else "success",
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "cache_read_tokens": usage["cache_read_tokens"],
        "cache_write_tokens": usage["cache_write_tokens"],
        "reasoning_tokens": usage["reasoning_tokens"],
        "latency_ms": _latency_ms(run.get("start_time"), run.get("end_time")),
        "cost_usd": _cost_usd(run),
        "error": error,
        "error_type": error_type,
        "request_id": run_id,
        "tags": tags,
        "route": route,
        "content_opted_in": True,
        "request_json": request_json,
        "request_text": request_text,
        "response_text": _response_text(run.get("outputs")),
        "trace_id": run.get("trace_id"),
        "span_id": run_id,
        "parent_span_id": run.get("parent_run_id"),
    }
    if import_context is not None:
        # The run id is LangSmith's stable identity for the call, so an
        # overlap re-pull deduplicates on the server.
        row["import_source"] = import_context.source
        row["import_source_scope"] = import_context.source_scope
        row["import_event_id"] = canonical_import_event_id(run.get("id"))
    return row


def _cleanup_temp_file(tmp_path: str) -> None:
    try:
        os.remove(tmp_path)
    except OSError:
        pass


def pull_langsmith(
    *,
    base_url: str,
    api_key: str,
    projects: list[str],
    count: int,
    since: str | None,
    until: str | None,
    names: list[str],
    tags: list[str],
    route: str | None,
    output_path: str,
    import_context: ImportContext | None = None,
    on_progress: Callable[[], None] | None = None,
) -> tuple[int, int]:
    """Pull up to ``count`` LLM runs from ``projects`` into ``output_path``."""
    if not projects:
        raise LangSmithAPIError("at least one project is required")
    project_ids = resolve_project_ids(base_url, api_key=api_key, projects=projects)
    if on_progress is not None:
        on_progress()
    imported = 0
    skipped = 0
    cursor: str | None = None
    used_cursors: set[str] = set()

    output_dir = os.path.dirname(output_path) or "."
    fd, tmp_path = tempfile.mkstemp(
        dir=output_dir, prefix=f".{os.path.basename(output_path)}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            while imported < count:
                if cursor:
                    if cursor in used_cursors:
                        raise LangSmithAPIError(
                            "LangSmith API returned a repeated pagination cursor "
                            "(non-advancing pagination), aborting to avoid an "
                            f"infinite loop: {cursor!r}"
                        )
                    used_cursors.add(cursor)
                body = build_query(
                    project_ids=project_ids, since=since, until=until, names=names,
                    tags=tags, limit=min(PAGE_LIMIT, count - imported), cursor=cursor,
                )
                runs, cursor = fetch_runs_page(base_url, api_key=api_key, body=body)
                if on_progress is not None:
                    on_progress()  # a page fetch is progress too
                if not runs:
                    break
                for run in runs:
                    if imported >= count:
                        break
                    try:
                        row = normalize_run(
                            run, route_override=route, import_context=import_context
                        )
                        line = json.dumps(row)
                    except (KeyError, TypeError, AttributeError) as exc:
                        skipped += 1
                        if on_progress is not None:
                            on_progress()
                        run_id = run.get("id", "<unknown>") if isinstance(run, dict) else "<unknown>"
                        print(
                            f"Warning: skipping malformed run {run_id}: {exc}",
                            file=sys.stderr,
                        )
                        continue
                    f.write(line + "\n")
                    imported += 1
                    if on_progress is not None:
                        on_progress()
                if not cursor:
                    break
        os.replace(tmp_path, output_path)
    finally:
        _cleanup_temp_file(tmp_path)
    return imported, skipped
