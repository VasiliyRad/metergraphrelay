from __future__ import annotations

import hashlib
from typing import Any

IMPORT_CONTRACT_VERSION = 1


def _stable(source: str, scope: str, value: Any, length: int) -> str:
    return hashlib.sha256(f"{source}:{scope}:{value}".encode()).hexdigest()[:length]


def with_import_provenance(
    row: dict,
    *,
    source: str,
    scope: str,
    event_id: Any,
    source_trace_id: Any,
) -> dict:
    """Attach Metergraph's version-1 source identity and stable trace ids."""
    if not scope.strip():
        raise ValueError("source scope cannot be empty")
    if event_id is None or str(event_id) == "":
        raise ValueError(f"{source} row is missing its event id")
    parent = row.get("parent_span_id")
    original_span = row.get("span_id") or event_id
    result = {
        **row,
        "import_source": source,
        "import_source_scope": scope,
        "import_event_id": str(event_id),
        "source_trace_id": str(source_trace_id or event_id),
        "source_span_id": str(original_span),
        "trace_id": _stable(source, scope, source_trace_id or event_id, 32),
        "span_id": _stable(source, scope, event_id, 16),
        "parent_span_id": _stable(source, scope, parent, 16) if parent else None,
    }
    if parent:
        result["source_parent_span_id"] = str(parent)
    return result
