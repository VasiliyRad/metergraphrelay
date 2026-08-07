from __future__ import annotations

import json
from typing import Any

DEFAULT_LANGFUSE_HOST = "https://cloud.langfuse.com"
OBSERVATIONS_PATH = "/api/public/v2/observations"
PAGE_LIMIT = 1000
# core+basic+time cover id/type/name/traceId/startTime/endTime/level/statusMessage/
# parentObservationId/sessionId; io covers input/output; usage covers usageDetails
# and totalCost; model covers providedModelName; trace_context denormalizes
# traceName/tags/environment/release onto each observation. Requesting all of them
# up front avoids silently missing a field the normalize step depends on.
RESPONSE_FIELDS = "core,basic,time,io,usage,model,trace_context"


def build_filter(trace_names: list[str], tags: list[str]) -> str | None:
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
    params: dict[str, str] = {
        "type": "GENERATION",
        "toStartTime": until,
        "fields": RESPONSE_FIELDS,
        "parseIoAsJson": "true",
    }
    if since:
        params["fromStartTime"] = since
    if environment:
        params["environment"] = environment
    filter_json = build_filter(trace_names, tags)
    if filter_json:
        params["filter"] = filter_json
    return params
