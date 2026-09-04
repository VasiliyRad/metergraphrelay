from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from .. import __version__
from ..import_identity import ImportContext, canonical_import_event_id

DEFAULT_LANGFUSE_HOST = "https://cloud.langfuse.com"
OBSERVATIONS_PATH = "/api/public/v2/observations"
PAGE_LIMIT = 1000
GENERATION_TYPE = "GENERATION"
# core+basic+time cover id/type/name/traceId/startTime/endTime/level/statusMessage/
# parentObservationId/sessionId; io covers input/output; usage covers usageDetails
# and totalCost; model covers the model name (returned as `model` in
# practice, with `providedModelName` as a fallback); metadata covers explicit
# provider metadata used for provider inference in a later task; trace_context
# denormalizes traceName/tags/environment/release onto each observation.
# Requesting all of them up front avoids silently missing a field the
# normalize step depends on.
RESPONSE_FIELDS = "core,basic,time,io,usage,model,metadata,trace_context"


def _selector_conditions(trace_names: list[str], tags: list[str]) -> list[dict[str, Any]]:
    conditions: list[dict[str, Any]] = []
    if trace_names:
        conditions.append(
            {
                "type": "stringOptions",
                "column": "traceName",
                "operator": "any of",
                "value": list(trace_names),
            }
        )
    if tags:
        conditions.append(
            {
                "type": "arrayOptions",
                "column": "tags",
                "operator": "all of",
                "value": list(tags),
            }
        )
    return conditions


def _encode_filter(conditions: list[dict[str, Any]]) -> str:
    return json.dumps(conditions)


def build_filter(trace_names: list[str], tags: list[str]) -> str | None:
    conditions = _selector_conditions(trace_names, tags)
    if not conditions:
        return None
    return _encode_filter(conditions)


def build_base_params(
    *,
    until: str,
    since: str | None,
    trace_names: list[str],
    tags: list[str],
    environment: str | None,
) -> dict[str, str]:
    params: dict[str, str] = {"fields": RESPONSE_FIELDS}
    conditions = _selector_conditions(trace_names, tags)
    if conditions:
        # Langfuse's structured `filter` param takes precedence over the
        # simpler type/environment/fromStartTime/toStartTime query params —
        # whenever a selector is present, those constraints must be folded
        # into this same filter array, or they are silently ignored.
        conditions.append(
            {
                "type": "stringOptions",
                "column": "type",
                "operator": "any of",
                "value": [GENERATION_TYPE],
            }
        )
        if since:
            conditions.append(
                {
                    "type": "datetime",
                    "column": "startTime",
                    "operator": ">=",
                    "value": since,
                }
            )
        conditions.append(
            {
                "type": "datetime",
                "column": "startTime",
                "operator": "<",
                "value": until,
            }
        )
        if environment:
            conditions.append(
                {
                    "type": "stringOptions",
                    "column": "environment",
                    "operator": "any of",
                    "value": [environment],
                }
            )
        params["filter"] = _encode_filter(conditions)
    else:
        params["type"] = GENERATION_TYPE
        params["toStartTime"] = until
        if since:
            params["fromStartTime"] = since
        if environment:
            params["environment"] = environment
    return params


class LangfuseAPIError(Exception):
    """Raised when Langfuse's API returns an error response or an unusable body."""


def _auth_header(public_key: str, secret_key: str) -> str:
    token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    return f"Basic {token}"


def fetch_observations_page(
    base_url: str,
    *,
    public_key: str,
    secret_key: str,
    params: dict[str, str],
) -> dict[str, Any]:
    query = urllib.parse.urlencode(params)
    url = f"{base_url.rstrip('/')}{OBSERVATIONS_PATH}"
    if query:
        url = f"{url}?{query}"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": _auth_header(public_key, secret_key),
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise LangfuseAPIError(
            f"Langfuse API request failed: HTTP {exc.code} {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise LangfuseAPIError(f"Langfuse API request failed: {exc.reason}") from exc
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise LangfuseAPIError(f"Langfuse API returned invalid JSON: {exc}") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    meta = payload.get("meta") if isinstance(payload, dict) else None
    if not isinstance(data, list) or not isinstance(meta, dict):
        raise LangfuseAPIError(
            "Langfuse API response missing/malformed 'data'/'meta' — unsupported "
            "deployment or unexpected response shape (self-hosted v4+ with the v2 "
            "Observations API is required)"
        )
    return payload


# Illustrative, not exhaustive — per the design spec's Mapping section, the
# concrete prefix table is an implementation-time task, not invented wholesale.
_PROVIDER_MODEL_PREFIXES: tuple[tuple[str, str], ...] = (
    ("gpt-", "openai"),
    ("o1-", "openai"),
    ("o3-", "openai"),
    ("chatgpt-", "openai"),
    ("claude-", "anthropic"),
    ("gemini-", "google"),
)


def _resolve_model_name(observation: dict[str, Any]) -> str | None:
    # Live v4 Observations API evidence: model comes back on `model`
    # (e.g. "gpt-4o-mini", "claude-3-5-haiku-latest"), with `providedModelName`
    # observed as None in practice. `providedModelName` is kept as a fallback
    # for robustness, not as the primary source.
    for key in ("model", "providedModelName"):
        value = observation.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def infer_provider(observation: dict[str, Any]) -> str:
    metadata = observation.get("metadata")
    if isinstance(metadata, dict):
        explicit = metadata.get("provider")
        if isinstance(explicit, str):
            explicit = explicit.strip().lower()
            if explicit:
                return explicit
    model_name = (_resolve_model_name(observation) or "").lower()
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


def _response_text(output_value: Any) -> str | None:
    # No response_json field exists in metergraph's native row — JSON-shaped
    # output is serialized into this single field instead of split like input.
    if output_value is None:
        return None
    if isinstance(output_value, str):
        return output_value
    return json.dumps(output_value)


# Langfuse stores usageDetails FLAT: its ingestion normalizes provider usage
# into one level of integer buckets and drops nested objects. Which keys appear
# depends on the integration that recorded the observation:
#
#   openai wrapper   -- ingestion renames prompt/completion to "input"/"output"
#                       and flattens details as input_cached_tokens /
#                       output_reasoning_tokens, each SUBTRACTED from its parent.
#   langchain        -- the callback handler flattens usage_metadata details as
#                       input_cache_read / input_cache_creation /
#                       output_reasoning, likewise subtracted.
#   gateway fallthrough -- a response the wrapper schema rejects keeps the raw
#                       prompt_tokens / completion_tokens, inclusive totals with
#                       the details dropped.
#   native SDK       -- whatever the caller passed to update(usage_details=...),
#                       e.g. Anthropic's input_tokens + cache_read_input_tokens.
#   gemini           -- promptTokenCount / candidatesTokenCount / thoughts_token_count.
#
# Langfuse's own contract is that every key is a non-overlapping bucket and the
# total is their sum. metergraph's convention is that input_tokens is the total
# with cache_read / cache_write / reasoning as subsets of it, so the named total
# key has every other *input* bucket added back (Langfuse itself sums by the
# "input" substring). Priority-tier buckets are pricing markers that overlap
# the parent, not exclusive sub-buckets, so they are left out of the sum.
_INPUT_TOTAL_KEYS = (
    "input", "input_tokens", "prompt_tokens", "promptTokenCount", "prompt_token_count",
)
_OUTPUT_TOTAL_KEYS = (
    "output", "output_tokens", "completion_tokens", "candidatesTokenCount",
    "candidates_token_count",
)
_CACHE_READ_KEYS = (
    "input_cached_tokens", "input_cache_read", "cache_read_input_tokens",
    "cachedContentTokenCount", "cached_content_token_count",
)
_CACHE_WRITE_KEYS = (
    "input_cache_creation_tokens", "input_cache_creation", "cache_creation_input_tokens",
)
_REASONING_KEYS = (
    "output_reasoning_tokens", "output_reasoning", "thoughtsTokenCount",
    "thoughts_token_count",
)
_NON_BUCKET_MARKERS = ("priority",)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _first_int_key(source: dict[str, Any], keys: tuple[str, ...]) -> tuple[str | None, int | None]:
    for key in keys:
        value = source.get(key)
        if _is_int(value):
            return key, value
    return None, None


def _first_int(source: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    return _first_int_key(source, keys)[1]


def _total_with_buckets(
    usage_details: dict[str, Any], total_keys: tuple[str, ...], marker: str
) -> int | None:
    """The named total plus every other integer bucket carrying ``marker``."""
    total_key, total = _first_int_key(usage_details, total_keys)
    if total_key is None:
        return None
    for key, value in usage_details.items():
        if key == total_key or not _is_int(value):
            continue
        lowered = key.lower()
        if marker not in lowered:
            continue
        if any(skip in lowered for skip in _NON_BUCKET_MARKERS):
            continue
        total += value
    return total


def map_usage_details(usage_details: Any) -> dict[str, int | None]:
    """Map one observation's usageDetails onto metergraph token fields."""
    if not isinstance(usage_details, dict):
        return {
            "input_tokens": None,
            "output_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "reasoning_tokens": None,
        }
    return {
        "input_tokens": _total_with_buckets(usage_details, _INPUT_TOTAL_KEYS, "input"),
        "output_tokens": _total_with_buckets(usage_details, _OUTPUT_TOTAL_KEYS, "output"),
        "cache_read_tokens": _first_int(usage_details, _CACHE_READ_KEYS),
        "cache_write_tokens": _first_int(usage_details, _CACHE_WRITE_KEYS),
        "reasoning_tokens": _first_int(usage_details, _REASONING_KEYS),
    }


def normalize_observation(
    observation: dict[str, Any],
    *,
    route_override: str | None,
    import_context: ImportContext | None = None,
) -> dict:
    trace_name = observation.get("traceName") or None
    own_name = observation.get("name") or None
    name_fallback = trace_name or own_name

    if route_override:
        route = route_override
        name_consumed = False
    else:
        route = name_fallback or ""
        name_consumed = True

    tags: dict[str, Any] = {}
    langfuse_tags = observation.get("tags")
    if isinstance(langfuse_tags, list) and langfuse_tags:
        tags["langfuse_tags"] = list(langfuse_tags)
    if not name_consumed and name_fallback:
        tags["name"] = name_fallback

    error = observation.get("level") == "ERROR"
    raw_status_message = observation.get("statusMessage")
    error_type = (
        raw_status_message if error and isinstance(raw_status_message, str) else None
    )

    usage = map_usage_details(observation.get("usageDetails"))
    request_json, request_text = _map_content(observation.get("input"))
    response_text = _response_text(observation.get("output"))

    model = _resolve_model_name(observation)

    row = {
        "ts": observation["startTime"],
        "source": "langfuse",
        "sdk": "metergraphrelay",
        "sdk_version": __version__,
        "provider": infer_provider(observation),
        "model": model,
        "status": "error" if error else "success",
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "cache_read_tokens": usage["cache_read_tokens"],
        "cache_write_tokens": usage["cache_write_tokens"],
        "reasoning_tokens": usage["reasoning_tokens"],
        "cost_usd": observation.get("totalCost"),
        "error": error,
        "error_type": error_type,
        "request_id": observation["id"],
        "tags": tags,
        "route": route,
        "content_opted_in": True,
        "request_json": request_json,
        "request_text": request_text,
        "response_text": response_text,
        "trace_id": observation["traceId"],
        "span_id": observation["id"],
        "parent_span_id": observation.get("parentObservationId"),
        "session_id": observation.get("sessionId"),
        "environment": observation.get("environment"),
    }
    if import_context is not None:
        # The observation id is Langfuse's stable identity for this generation,
        # so an overlap re-pull deduplicates on the server instead of double
        # counting.
        row["import_source"] = import_context.source
        row["import_source_scope"] = import_context.source_scope
        row["import_event_id"] = canonical_import_event_id(observation.get("id"))
    return row


def _cleanup_temp_file(tmp_path: str) -> None:
    try:
        os.remove(tmp_path)
    except OSError:
        pass


def pull_langfuse(
    *,
    base_url: str,
    public_key: str,
    secret_key: str,
    count: int,
    since: str | None,
    until: str,
    trace_names: list[str],
    tags: list[str],
    environment: str | None,
    route: str | None,
    output_path: str,
    import_context: ImportContext | None = None,
    on_progress: Callable[[], None] | None = None,
) -> tuple[int, int]:
    base_params = build_base_params(
        until=until,
        since=since,
        trace_names=trace_names,
        tags=tags,
        environment=environment,
    )
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
                page_params = dict(base_params)
                page_params["limit"] = str(min(PAGE_LIMIT, count - imported))
                if cursor:
                    if cursor in used_cursors:
                        raise LangfuseAPIError(
                            "Langfuse API returned a repeated pagination cursor "
                            "(non-advancing pagination), aborting to avoid an "
                            f"infinite loop: {cursor!r}"
                        )
                    used_cursors.add(cursor)
                    page_params["cursor"] = cursor
                payload = fetch_observations_page(
                    base_url,
                    public_key=public_key,
                    secret_key=secret_key,
                    params=page_params,
                )
                observations = payload["data"]
                if not observations:
                    break
                for observation in observations:
                    if imported >= count:
                        break
                    try:
                        row = normalize_observation(
                            observation,
                            route_override=route,
                            import_context=import_context,
                        )
                        line = json.dumps(row)
                    except (KeyError, TypeError, AttributeError) as exc:
                        skipped += 1
                        obs_id = (
                            observation.get("id", "<unknown>")
                            if isinstance(observation, dict)
                            else "<unknown>"
                        )
                        print(
                            f"Warning: skipping malformed observation {obs_id}: {exc}",
                            file=sys.stderr,
                        )
                        continue
                    f.write(line + "\n")
                    imported += 1
                    if on_progress is not None:
                        on_progress()
                raw_cursor = payload.get("meta", {}).get("cursor")
                if raw_cursor is not None and not (
                    isinstance(raw_cursor, str) and raw_cursor
                ):
                    raise LangfuseAPIError(
                        "Langfuse API returned a malformed pagination cursor: "
                        f"{raw_cursor!r}"
                    )
                cursor = raw_cursor
                if not cursor:
                    break
        os.replace(tmp_path, output_path)
    finally:
        # On success, tmp_path has already been moved to output_path by
        # os.replace above, so this is a harmless no-op (_cleanup_temp_file
        # silently ignores a missing path); on any failure, this removes the
        # still-present temp file before the exception propagates.
        _cleanup_temp_file(tmp_path)
    return imported, skipped
