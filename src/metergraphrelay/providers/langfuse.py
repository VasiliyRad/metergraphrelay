from __future__ import annotations

import json
from typing import Any

DEFAULT_LANGFUSE_HOST = "https://cloud.langfuse.com"
OBSERVATIONS_PATH = "/api/public/v2/observations"
PAGE_LIMIT = 1000
# core+basic+time cover id/type/name/traceId/startTime/endTime/level/statusMessage/
# parentObservationId/sessionId; io covers input/output; usage covers usageDetails
# and totalCost; model covers providedModelName; metadata covers explicit
# provider metadata (see infer_provider); trace_context denormalizes
# traceName/tags/environment/release onto each observation. Requesting all of them
# up front avoids silently missing a field the normalize step depends on.
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


def build_filter(trace_names: list[str], tags: list[str]) -> str | None:
    conditions = _selector_conditions(trace_names, tags)
    if not conditions:
        return None
    return json.dumps(conditions)


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
                "value": ["GENERATION"],
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
        params["filter"] = json.dumps(conditions)
    else:
        params["type"] = "GENERATION"
        params["toStartTime"] = until
        if since:
            params["fromStartTime"] = since
        if environment:
            params["environment"] = environment
    return params
