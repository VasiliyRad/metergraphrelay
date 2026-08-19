from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

SPLIT_PARTS = 10
SPLIT_OVERLAP_SECONDS = 1


@dataclass(frozen=True)
class TimeWindow:
    start: str  # aware ISO 8601
    end: str    # aware ISO 8601


def normalize_utc_designator(value: str) -> str:
    """Return ``value`` with a trailing ``Z``/``z`` UTC designator rewritten as ``+00:00``.

    Python 3.10's :func:`datetime.fromisoformat` rejects the ``Z`` suffix that 3.11+
    accepts; normalizing here keeps aware-timestamp parsing identical across versions.
    A value already carrying an explicit offset is returned unchanged.
    """
    if value.endswith(("Z", "z")):
        return f"{value[:-1]}+00:00"
    return value


def _parse_aware(value: str) -> datetime:
    dt = datetime.fromisoformat(normalize_utc_designator(value))
    if dt.tzinfo is None:
        raise ValueError(f"timestamp must be timezone-aware, got naive value: {value!r}")
    return dt


def split_window(
    window: TimeWindow, *, parts: int = SPLIT_PARTS, overlap_seconds: int = SPLIT_OVERLAP_SECONDS
) -> list[TimeWindow]:
    start = _parse_aware(window.start)
    end = _parse_aware(window.end)
    if end <= start:
        raise ValueError(f"window end {window.end!r} must be after start {window.start!r}")
    total = (end - start) / parts
    overlap = timedelta(seconds=overlap_seconds)
    result: list[TimeWindow] = []
    for i in range(parts):
        base_start = start + total * i
        base_end = start + total * (i + 1) if i < parts - 1 else end
        sub_start = base_start - overlap if i > 0 else start
        result.append(TimeWindow(start=sub_start.isoformat(), end=base_end.isoformat()))
    return result
