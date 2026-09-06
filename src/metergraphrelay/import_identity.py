"""Import provenance shared by every server-coordinated sync path.

A synced row carries ``import_source`` / ``import_source_scope`` /
``import_event_id`` so the metergraph server can deduplicate a row that is
re-pulled inside a window overlap. The server's identity validator requires
the event id to be a string whose stripped length is 1..512; a numeric,
blank, or oversized id would make the async import worker reject the whole
batch *after* the relay has uploaded it and completed the lease, losing the
window silently. ``canonical_import_event_id`` rejects such an id in the
relay, and ``ImportIdentityError`` is a ``ValueError`` on purpose: the
providers' per-row "malformed data" skip paths catch only KeyError /
TypeError / AttributeError, so an invalid identity fails the window instead
of being skipped past.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

IMPORT_EVENT_ID_MAX_LENGTH = 512


class ImportIdentityError(ValueError):
    """A row cannot carry a valid import identity; the window must not complete."""


@dataclass(frozen=True)
class ImportContext:
    source: str
    source_scope: str


def canonical_import_event_id(raw: Any) -> str:
    """Validate a provider's stable row id for use as ``import_event_id``.

    ``bool`` is an ``int`` subclass, so it is rejected as a non-string.
    """
    if not isinstance(raw, str):
        raise ImportIdentityError(
            f"import_event_id must be a string, got {type(raw).__name__}"
        )
    canonical = raw.strip()
    if not 1 <= len(canonical) <= IMPORT_EVENT_ID_MAX_LENGTH:
        raise ImportIdentityError(
            "import_event_id must be 1.."
            f"{IMPORT_EVENT_ID_MAX_LENGTH} characters after stripping, "
            f"got length {len(canonical)}"
        )
    return canonical
