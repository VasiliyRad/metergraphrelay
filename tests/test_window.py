import pytest

from metergraphrelay.window import SPLIT_OVERLAP_SECONDS, SPLIT_PARTS, TimeWindow, split_window


def test_split_window_returns_exactly_ten_intervals_by_default():
    w = TimeWindow(start="2026-08-19T00:00:00+00:00", end="2026-08-19T01:00:00+00:00")
    parts = split_window(w)
    assert len(parts) == SPLIT_PARTS == 10


def test_split_window_first_start_and_last_end_match_the_original_window():
    w = TimeWindow(start="2026-08-19T00:00:00+00:00", end="2026-08-19T01:00:00+00:00")
    parts = split_window(w)
    assert parts[0].start == "2026-08-19T00:00:00+00:00"
    assert parts[-1].end == "2026-08-19T01:00:00+00:00"


def test_split_window_internal_boundaries_overlap_by_one_second():
    # A one-hour window into 10 parts => 6-minute (360s) base intervals.
    # Each interval after the first starts SPLIT_OVERLAP_SECONDS before the
    # previous interval's end, producing a 1-second overlap at every internal
    # boundary. Boundary duplicates are absorbed by source-event idempotency.
    w = TimeWindow(start="2026-08-19T00:00:00+00:00", end="2026-08-19T01:00:00+00:00")
    parts = split_window(w)
    assert parts[0].end == "2026-08-19T00:06:00+00:00"
    assert parts[1].start == "2026-08-19T00:05:59+00:00"  # 1s before parts[0].end
    assert parts[1].end == "2026-08-19T00:12:00+00:00"


def test_split_window_preserves_utc_offset_of_inputs():
    w = TimeWindow(start="2026-08-19T00:00:00+00:00", end="2026-08-19T01:00:00+00:00")
    for part in split_window(w):
        assert part.start.endswith("+00:00")
        assert part.end.endswith("+00:00")


def test_split_window_rejects_naive_timestamps():
    w = TimeWindow(start="2026-08-19T00:00:00", end="2026-08-19T01:00:00")
    with pytest.raises(ValueError, match="aware"):
        split_window(w)


def test_split_window_rejects_end_not_after_start():
    w = TimeWindow(start="2026-08-19T01:00:00+00:00", end="2026-08-19T00:00:00+00:00")
    with pytest.raises(ValueError):
        split_window(w)
