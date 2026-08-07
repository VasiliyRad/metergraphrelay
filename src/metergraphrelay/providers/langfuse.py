from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .. import __version__

DEFAULT_LANGFUSE_HOST = "https://cloud.langfuse.com"
OBSERVATIONS_PATH = "/api/public/v2/observations"
PAGE_LIMIT = 1000
GENERATION_TYPE = "GENERATION"
# core+basic+time cover id/type/name/traceId/startTime/endTime/level/statusMessage/
# parentObservationId/sessionId; io covers input/output; usage covers usageDetails
# and totalCost; model covers providedModelName; metadata covers explicit
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


def infer_provider(observation: dict[str, Any]) -> str:
    metadata = observation.get("metadata")
    if isinstance(metadata, dict):
        explicit = metadata.get("provider")
        if isinstance(explicit, str):
            explicit = explicit.strip().lower()
            if explicit:
                return explicit
    raw_model_name = observation.get("providedModelName")
    model_name = raw_model_name.lower() if isinstance(raw_model_name, str) else ""
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


def normalize_observation(
    observation: dict[str, Any], *, route_override: str | None
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
    if langfuse_tags:
        tags["langfuse_tags"] = list(langfuse_tags)
    if not name_consumed and name_fallback:
        tags["name"] = name_fallback

    error = observation.get("level") == "ERROR"
    error_type = observation.get("statusMessage") if error else None

    usage_details = observation.get("usageDetails") or {}
    request_json, request_text = _map_content(observation.get("input"))
    response_text = _response_text(observation.get("output"))

    return {
        "ts": observation["startTime"],
        "source": "langfuse",
        "sdk": "metergraphrelay",
        "sdk_version": __version__,
        "provider": infer_provider(observation),
        "model": observation.get("providedModelName"),
        "status": "error" if error else "success",
        "input_tokens": usage_details.get("input"),
        "output_tokens": usage_details.get("output"),
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
