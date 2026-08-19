"""Tests for the CLI-independent Portkey sync orchestrator (Task 5).

These drive ``run_portkey_sync`` against in-memory fakes for the MeterGraph
import-sync client, the Portkey Logs Export client, and the push function, so the
whole acquire -> plan -> start -> poll(+renew) -> download -> normalize -> push ->
complete lifecycle is exercised without any network. Lease renewal is time-based
(driven by an injected monotonic clock), so the clock is advanced explicitly to
prove renewals happen well within the lease duration without flooding.
"""

import json
import os

import pytest

from metergraphrelay.metergraph_sync import (
    AcquiredLease,
    AcquireResult,
    LeaseLostError,
    MeterGraphSyncError,
)
from metergraphrelay.providers.portkey_export import (
    PAGE_SIZE_MAX,
    STATUS_DRAFT,
    STATUS_FAILED,
    STATUS_IN_PROGRESS,
    STATUS_SUCCESS,
    PortkeyExport,
    PortkeyExportError,
)
from metergraphrelay.portkey_sync import (
    RENEW_INTERVAL_SECONDS,
    VOLUME_SPLIT_THRESHOLD,
    _LeaseRenewer,
    run_portkey_sync,
)

WINDOW_START = "2026-08-19T00:00:00+00:00"
WINDOW_END = "2026-08-19T01:00:00+00:00"
LEASE_SECONDS = 900.0  # the server's 15-minute lease; renewal must beat this


class FakeClock:
    """A hand-advanced monotonic clock for deterministic renewal tests."""

    def __init__(self, start=0.0):
        self._t = start

    def __call__(self):
        return self._t

    def advance(self, dt):
        self._t += dt


def _acquired():
    return AcquireResult(
        status="acquired",
        lease=AcquiredLease(
            lease_id="lease-1", checkpoint_version=1,
            window_start=WINDOW_START, window_end=WINDOW_END,
            lease_expires_at="2026-08-19T00:15:00+00:00",
        ),
    )


def _portkey_row(rid):
    # Minimal shape convert_portkey_export accepts (see providers/portkey.py).
    return {
        "id": rid, "trace_id": f"t-{rid}", "created_at": WINDOW_START,
        "ai_org": "openai", "ai_model": "gpt-5", "cost": 10.0,
        "req_units": 1, "res_units": 1, "response_time": 100, "response_status_code": 200,
        "request": {"model": "gpt-5", "input": "hi"},
        "response": {"object": "response", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]},
        "metadata": {"workflow_name": "wf"},
    }


class FakeMeterGraph:
    def __init__(self, acquire_result):
        self._acquire = acquire_result
        self.acquire_error = None
        self.acquire_kwargs = None
        self.renewed = 0
        self.completed = []
        self.abandoned = []
        self.renew_error = None
        self.complete_error = None
        self.events = []  # ordered log of ("renew"|"complete"|"abandon", ...)

    def acquire(self, **kwargs):
        self.acquire_kwargs = kwargs
        if self.acquire_error:
            raise self.acquire_error
        return self._acquire

    def renew(self, lease_id):
        if self.renew_error:
            raise self.renew_error
        self.renewed += 1
        self.events.append(("renew", lease_id))
        return "2026-08-19T00:30:00+00:00"

    def complete(self, lease_id):
        if self.complete_error:
            raise self.complete_error
        self.completed.append(lease_id)
        self.events.append(("complete", lease_id))

    def abandon(self, lease_id):
        self.abandoned.append(lease_id)
        self.events.append(("abandon", lease_id))


class FakePortkey:
    """create_export returns the draft total immediately; get_export reports success.

    Rows are keyed by (window_start, window_end); ``totals`` overrides the count a
    given window's draft reports, and ``default_total`` overrides every window's.
    """

    def __init__(self, rows_by_window, totals=None, default_total=None):
        self._rows = rows_by_window
        self._totals = totals or {}
        self._default_total = default_total
        self.created = []       # list of (window_start, window_end)
        self.started = []       # export_ids
        self.cancelled = []     # export_ids
        self._win_by_id = {}
        self._seq = 0

    def _total_for(self, key):
        if key in self._totals:
            return self._totals[key]
        if self._default_total is not None:
            return self._default_total
        return len(self._rows.get(key, []))

    def create_export(self, *, window_start, window_end):
        self._seq += 1
        eid = f"exp-{self._seq}"
        key = (window_start, window_end)
        self.created.append(key)
        self._win_by_id[eid] = key
        return PortkeyExport(export_id=eid, total=self._total_for(key), status=STATUS_DRAFT)

    def start_export(self, export_id):
        self.started.append(export_id)

    def get_export(self, export_id):
        return PortkeyExport(export_id=export_id, total=None, status=STATUS_SUCCESS)

    def cancel_export(self, export_id):
        self.cancelled.append(export_id)

    def download_to(self, export_id, dest_path, *, on_progress=None):
        rows = self._rows.get(self._win_by_id[export_id], [])
        with open(dest_path, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        return len(rows)


def _run(mg, pk, pushes, **overrides):
    def fake_push(path, token, base_url=None, *, on_progress=None):
        rows = [json.loads(line) for line in open(path).read().splitlines() if line.strip()]
        for _ in rows:
            if on_progress is not None:
                on_progress()
        pushes.append({"token": token, "base_url": base_url, "rows": rows, "path": path})
        return (len(rows), 0)

    kwargs = dict(
        mg_client=mg, pk_client=pk, source_scope="ws-acme",
        initial_since="2026-08-01T00:00:00+00:00", max_window_seconds=3600,
        push_token="tok-123", ingest_base_url=None,
        sleep=lambda _s: None, push=fake_push,
    )
    kwargs.update(overrides)
    return run_portkey_sync(**kwargs)


# -- _LeaseRenewer: time-based renewal policy ------------------------------


def test_lease_renewer_does_not_renew_before_interval_elapses():
    clock = FakeClock()
    renews = []
    r = _LeaseRenewer(lambda: renews.append(1), clock=clock, interval=RENEW_INTERVAL_SECONDS)
    for _ in range(1000):  # 1000 ticks, but only 100s of wall time
        clock.advance(0.1)
        r.tick()
    assert renews == []  # frequent ticks over a short span never flood a renewal


def test_lease_renewer_renews_once_per_interval_regardless_of_tick_rate():
    clock = FakeClock()
    renews = []
    r = _LeaseRenewer(lambda: renews.append(1), clock=clock, interval=300.0)
    for _ in range(3000):  # 3000s of wall time, one tick per second
        clock.advance(1.0)
        r.tick()
    # A renewal at 300, 600, ... 3000 -> ten, NOT 3000. No flooding; each gap < lease.
    assert len(renews) == 10
    assert len(renews) * 300.0 <= 3000.0
    assert 300.0 < LEASE_SECONDS  # the cadence beats the lease with margin


def test_lease_renewer_renews_on_a_slow_phase_before_the_lease_lapses():
    # A slow reader that only ticks occasionally still renews each time the
    # interval has elapsed, so a 900s lease never lapses between ticks.
    clock = FakeClock()
    renews = []
    r = _LeaseRenewer(lambda: renews.append(1), clock=clock, interval=RENEW_INTERVAL_SECONDS)
    for _ in range(5):
        clock.advance(RENEW_INTERVAL_SECONDS + 1.0)
        r.tick()
    assert len(renews) == 5


def test_lease_renewer_force_renews_unconditionally_and_resets_the_timer():
    clock = FakeClock()
    renews = []
    r = _LeaseRenewer(lambda: renews.append(1), clock=clock, interval=300.0)
    r.force()
    assert len(renews) == 1              # forced despite no time elapsed
    clock.advance(299.0)
    r.tick()
    assert len(renews) == 1              # timer was reset by force -> not yet due
    clock.advance(1.0)
    r.tick()
    assert len(renews) == 2              # 300s since the force -> due again


def test_lease_renewer_propagates_a_renew_exception_from_a_tick():
    clock = FakeClock()

    def boom():
        raise LeaseLostError("lease gone")

    r = _LeaseRenewer(boom, clock=clock, interval=300.0)
    clock.advance(300.0)
    with pytest.raises(LeaseLostError):
        r.tick()


# -- no-op exits -----------------------------------------------------------


def test_caught_up_is_a_clean_noop_exit():
    mg = FakeMeterGraph(AcquireResult(status="caught_up"))
    pk = FakePortkey({})
    pushes = []
    outcome = _run(mg, pk, pushes)
    assert outcome.status == "caught_up"
    assert outcome.exit_code == 0
    assert pk.created == []
    assert mg.completed == []
    assert mg.abandoned == []


def test_busy_is_a_clean_noop_exit_and_surfaces_retry_at():
    mg = FakeMeterGraph(AcquireResult(status="busy", retry_at="2026-08-19T00:20:00+00:00"))
    outcome = _run(mg, FakePortkey({}), [])
    assert outcome.status == "busy"
    assert outcome.exit_code == 0
    assert "2026-08-19T00:20:00+00:00" in outcome.detail


def test_acquire_receives_source_scope_initial_since_and_max_window():
    mg = FakeMeterGraph(AcquireResult(status="caught_up"))
    _run(mg, FakePortkey({}), [])
    assert mg.acquire_kwargs == {
        "source": "portkey", "source_scope": "ws-acme",
        "initial_since": "2026-08-01T00:00:00+00:00", "max_window_seconds": 3600,
    }


# -- acquire failures (no lease exists -> never abandon) -------------------


def test_acquire_network_failure_returns_failed_without_abandon():
    mg = FakeMeterGraph(AcquireResult(status="caught_up"))
    mg.acquire_error = MeterGraphSyncError("acquire failed: HTTP 500 err")
    pk = FakePortkey({})

    outcome = _run(mg, pk, [])

    assert outcome.status == "failed"
    assert outcome.exit_code == 1
    assert mg.abandoned == []       # no lease was ever held
    assert pk.created == []          # never reached Portkey
    assert "500" in outcome.detail


def test_acquire_acquired_without_lease_is_handled_cleanly():
    mg = FakeMeterGraph(AcquireResult(status="acquired", lease=None))

    outcome = _run(mg, FakePortkey({}), [])

    assert outcome.status == "failed"
    assert outcome.exit_code == 1
    assert mg.abandoned == []       # no lease object -> nothing to release


def test_acquire_unknown_status_is_handled_cleanly():
    mg = FakeMeterGraph(AcquireResult(status="teapot"))

    outcome = _run(mg, FakePortkey({}), [])

    assert outcome.status == "failed"
    assert outcome.exit_code == 1
    assert mg.abandoned == []


# -- happy path (unsplit) --------------------------------------------------


def test_end_to_end_happy_path_creates_starts_downloads_normalizes_pushes_and_completes():
    rows = [_portkey_row("r1"), _portkey_row("r2")]
    pk = FakePortkey({(WINDOW_START, WINDOW_END): rows})
    mg = FakeMeterGraph(_acquired())
    pushes = []

    outcome = _run(mg, pk, pushes)

    assert outcome.status == "completed"
    assert outcome.exit_code == 0
    assert pk.created == [(WINDOW_START, WINDOW_END)]
    assert pk.started == ["exp-1"]            # the single hourly draft was started
    assert pk.cancelled == []                  # nothing cancelled on the happy path
    assert mg.completed == ["lease-1"]         # complete only after a successful push
    assert mg.abandoned == []
    assert mg.renewed >= 1                      # renewed at least once during long phases
    # Pushed rows carry the server-dedup fields:
    pushed_rows = [r for p in pushes for r in p["rows"]]
    assert {r["import_event_id"] for r in pushed_rows} == {"r1", "r2"}
    assert all(r["import_source"] == "portkey" for r in pushed_rows)
    assert all(r["import_source_scope"] == "ws-acme" for r in pushed_rows)
    assert pushes[0]["token"] == "tok-123"
    assert outcome.pushed == 2


# -- volume split ----------------------------------------------------------


def test_volume_split_threshold_is_the_export_page_size_max():
    # Single source of truth: create_export fetches exactly one page of PAGE_SIZE_MAX
    # rows, so any window whose draft total exceeds PAGE_SIZE_MAX cannot fit in one
    # page and MUST split. Deriving the threshold from PAGE_SIZE_MAX makes silent
    # data loss (threshold drifting above the page size) structurally impossible.
    assert VOLUME_SPLIT_THRESHOLD == PAGE_SIZE_MAX


def test_window_one_over_page_size_max_splits_and_exactly_at_it_does_not():
    # Behaviour keyed off PAGE_SIZE_MAX (not a hardcoded 50_000): total == page size
    # uses the hourly draft as-is; total == page size + 1 triggers the 10-way split.
    full = (WINDOW_START, WINDOW_END)

    at_limit = FakePortkey({full: [_portkey_row("r1")]}, totals={full: PAGE_SIZE_MAX})
    outcome = _run(FakeMeterGraph(_acquired()), at_limit, [])
    assert outcome.status == "completed"
    assert at_limit.created == [full]          # no split exactly at the page size
    assert at_limit.cancelled == []

    over = FakePortkey({}, totals={full: PAGE_SIZE_MAX + 1})
    original_create = over.create_export

    def seeding_create(*, window_start, window_end):
        over._rows.setdefault((window_start, window_end), [_portkey_row(f"r-{window_start}")])
        return original_create(window_start=window_start, window_end=window_end)

    over.create_export = seeding_create
    outcome = _run(FakeMeterGraph(_acquired()), over, [])
    assert outcome.status == "completed"
    assert len(over.created) == 1 + 10         # one page over the max -> split into ten
    assert over.cancelled == ["exp-1"]


# -- volume split (threshold constant) -------------------------------------


def test_over_threshold_cancels_unstarted_hourly_draft_and_splits_into_ten():
    full = (WINDOW_START, WINDOW_END)
    # The hourly DRAFT's create response reports > 50k -> cancel it (unstarted),
    # then create 10 overlapping sub-window drafts (each small).
    pk = FakePortkey({}, totals={full: VOLUME_SPLIT_THRESHOLD + 1})

    # Seed one row per sub-window as the orchestrator creates them, so downloads
    # and pushes have content. Sub-window drafts default to len(rows) == 1 (<=50k).
    original_create = pk.create_export

    def seeding_create(*, window_start, window_end):
        pk._rows.setdefault((window_start, window_end), [_portkey_row(f"r-{window_start}")])
        return original_create(window_start=window_start, window_end=window_end)

    pk.create_export = seeding_create

    mg = FakeMeterGraph(_acquired())
    pushes = []
    outcome = _run(mg, pk, pushes)

    assert outcome.status == "completed"
    # 1 hourly draft + 10 sub-window drafts, exactly one split (no recursion).
    assert pk.created[0] == full
    assert len(pk.created) == 1 + 10
    assert pk.cancelled == ["exp-1"]                 # the unstarted hourly draft, only
    assert pk.started == [f"exp-{i}" for i in range(2, 12)]  # only the ten sub-windows start
    sub_windows = pk.created[1:]
    assert sub_windows[0][0] == WINDOW_START          # first sub-window starts at window start
    assert sub_windows[-1][1] == WINDOW_END           # last sub-window ends at window end
    assert mg.completed == ["lease-1"]                 # completed only after all ten pushed


def test_boundary_total_exactly_at_threshold_does_not_split():
    # total == 50_000 is NOT over the threshold: the hourly draft is used as-is.
    full = (WINDOW_START, WINDOW_END)
    pk = FakePortkey({full: [_portkey_row("r1")]}, totals={full: VOLUME_SPLIT_THRESHOLD})
    mg = FakeMeterGraph(_acquired())

    outcome = _run(mg, pk, [])

    assert outcome.status == "completed"
    assert pk.created == [full]          # no split at exactly the threshold
    assert pk.started == ["exp-1"]
    assert pk.cancelled == []
    assert mg.completed == ["lease-1"]


def test_subwindow_still_over_threshold_is_rejected_without_recursion():
    # Every window (hourly and sub-windows) reports > 50k -> after the split the
    # first sub-window is still oversized -> clear rejection, no recursive split.
    pk = FakePortkey({}, default_total=VOLUME_SPLIT_THRESHOLD + 1)
    mg = FakeMeterGraph(_acquired())

    outcome = _run(mg, pk, [])

    assert outcome.status == "failed"
    assert outcome.exit_code == 1
    assert mg.completed == []
    assert mg.abandoned == ["lease-1"]                 # handled failure releases the lease
    assert "exp-1" in pk.cancelled                      # unstarted hourly draft cancelled
    assert pk.started == []                             # nothing was ever started
    assert str(VOLUME_SPLIT_THRESHOLD) in outcome.detail or "recursiv" in outcome.detail.lower()


def test_planning_valueerror_from_bad_window_bounds_is_handled_failure():
    # The server hands out reversed window bounds and an oversized draft, so the
    # split planner calls split_window with end <= start and it raises ValueError.
    # That must surface as a handled sync failure (best-effort cancel + abandon +
    # exit 1), never an unhandled traceback that leaks the lease.
    reversed_lease = AcquireResult(
        status="acquired",
        lease=AcquiredLease(
            lease_id="lease-1", checkpoint_version=1,
            window_start=WINDOW_END, window_end=WINDOW_START,  # reversed -> ValueError
            lease_expires_at="2026-08-19T00:15:00+00:00",
        ),
    )
    full = (WINDOW_END, WINDOW_START)
    pk = FakePortkey({}, totals={full: VOLUME_SPLIT_THRESHOLD + 1})
    mg = FakeMeterGraph(reversed_lease)

    outcome = _run(mg, pk, [])

    assert outcome.status == "failed"
    assert outcome.exit_code == 1
    assert mg.completed == []
    assert mg.abandoned == ["lease-1"]          # handled failure releases the lease
    assert "exp-1" in pk.cancelled              # unstarted hourly draft best-effort cancelled
    assert pk.started == []                      # nothing was ever started


def test_split_does_not_accumulate_staging_files_across_subwindows(tmp_path):
    # With a 10-way split, raw and converted files must not pile up: each raw file
    # is deleted right after normalization and each converted file right after its
    # push. At the moment any sub-window is pushed, staging should hold only that
    # one converted file — never earlier raw/converted files.
    full = (WINDOW_START, WINDOW_END)
    pk = FakePortkey({}, totals={full: VOLUME_SPLIT_THRESHOLD + 1})
    original_create = pk.create_export

    def seeding_create(*, window_start, window_end):
        pk._rows.setdefault((window_start, window_end), [_portkey_row(f"r-{window_start}")])
        return original_create(window_start=window_start, window_end=window_end)

    pk.create_export = seeding_create
    mg = FakeMeterGraph(_acquired())

    snapshots = []  # staging dir contents captured at each push

    def snapshotting_push(path, token, base_url=None, *, on_progress=None):
        snapshots.append(sorted(os.listdir(os.path.dirname(path))))
        rows = [json.loads(line) for line in open(path).read().splitlines() if line.strip()]
        return (len(rows), 0)

    outcome = _run(mg, pk, [], push=snapshotting_push, work_dir=str(tmp_path))

    assert outcome.status == "completed"
    assert len(snapshots) == 10                      # one push per sub-window
    for i, contents in enumerate(snapshots):
        # Only the converted file currently being pushed is present: no leftover raw
        # files (deleted after normalize) and no earlier converted files.
        assert contents == [f"converted-{i}.jsonl"], contents


# -- failure handling ------------------------------------------------------


def test_push_failure_abandons_lease_and_exits_nonzero_without_completing():
    rows = [_portkey_row("r1")]
    pk = FakePortkey({(WINDOW_START, WINDOW_END): rows})
    mg = FakeMeterGraph(_acquired())

    def failing_push(path, token, base_url=None, *, on_progress=None):
        return (0, 1)  # one row failed

    outcome = _run(mg, pk, [], push=failing_push)

    assert outcome.status == "failed"
    assert outcome.exit_code == 1
    assert outcome.failed == 1
    assert mg.completed == []
    assert mg.abandoned == ["lease-1"]        # handled failure releases the lease


def test_conversion_failure_abandons_lease_and_exits_nonzero():
    # A row whose id is not a usable import_event_id makes convert_portkey_export
    # raise PortkeyConversionError; the whole window fails (never silently dropped).
    pk = FakePortkey({(WINDOW_START, WINDOW_END): [_portkey_row(12345)]})  # numeric id
    mg = FakeMeterGraph(_acquired())

    outcome = _run(mg, pk, [])

    assert outcome.status == "failed"
    assert outcome.exit_code == 1
    assert mg.completed == []
    assert mg.abandoned == ["lease-1"]
    assert "exp-1" in pk.cancelled            # best-effort cancel of the created export


def test_invalid_provider_timestamp_fails_window_before_upload():
    row = _portkey_row("r1")
    row["created_at"] = "not-a-timestamp"
    pk = FakePortkey({(WINDOW_START, WINDOW_END): [row]})
    mg = FakeMeterGraph(_acquired())
    pushes = []

    outcome = _run(mg, pk, pushes)

    assert outcome.status == "failed"
    assert outcome.exit_code == 1
    assert "created_at" in outcome.detail
    assert pushes == []
    assert mg.completed == []
    assert mg.abandoned == ["lease-1"]


def test_portkey_error_best_effort_cancels_and_abandons_lease():
    mg = FakeMeterGraph(_acquired())

    class Boom(FakePortkey):
        def create_export(self, *, window_start, window_end):
            raise PortkeyExportError("create boom")

    outcome = _run(mg, Boom({}), [])
    assert outcome.status == "failed"
    assert outcome.exit_code == 1
    assert mg.abandoned == ["lease-1"]


def test_best_effort_cancel_swallows_cancel_errors_without_masking_primary_failure():
    rows = [_portkey_row("r1")]

    class CancelBoom(FakePortkey):
        def get_export(self, export_id):
            # Report a terminal failure so the poll phase raises the primary error.
            return PortkeyExport(export_id=export_id, total=None, status=STATUS_FAILED)

        def cancel_export(self, export_id):
            self.cancelled.append(export_id)
            raise PortkeyExportError("cancel boom")  # must be swallowed

    pk = CancelBoom({(WINDOW_START, WINDOW_END): rows})
    mg = FakeMeterGraph(_acquired())

    outcome = _run(mg, pk, [])

    assert outcome.status == "failed"
    assert outcome.exit_code == 1
    assert "exp-1" in pk.cancelled            # cancel was attempted
    assert mg.abandoned == ["lease-1"]        # abandon still happened despite cancel error
    assert "did not succeed" in outcome.detail.lower() or "fail" in outcome.detail.lower()


def test_export_terminal_failure_status_abandons_lease_and_reports_clearly():
    class FailingExports(FakePortkey):
        def get_export(self, export_id):
            return PortkeyExport(export_id=export_id, total=None, status=STATUS_FAILED)

    pk = FailingExports({(WINDOW_START, WINDOW_END): [_portkey_row("r1")]})
    mg = FakeMeterGraph(_acquired())

    outcome = _run(mg, pk, [])

    assert outcome.status == "failed"
    assert outcome.exit_code == 1
    assert mg.completed == []
    assert mg.abandoned == ["lease-1"]
    assert "exp-1" in outcome.detail          # names the offending export


def test_poll_timeout_abandons_lease_and_reports_safety_cap():
    class StuckExports(FakePortkey):
        def get_export(self, export_id):
            return PortkeyExport(export_id=export_id, total=None, status=STATUS_IN_PROGRESS)

    pk = StuckExports({(WINDOW_START, WINDOW_END): [_portkey_row("r1")]})
    mg = FakeMeterGraph(_acquired())

    outcome = _run(
        mg, pk, [], poll_interval_seconds=15.0, max_poll_seconds=15.0,
    )

    assert outcome.status == "failed"
    assert outcome.exit_code == 1
    assert mg.completed == []
    assert mg.abandoned == ["lease-1"]
    assert "cap" in outcome.detail.lower() or "15" in outcome.detail


# -- lease renewal (time-based, driven through every long phase) -----------


def test_lease_renewed_each_round_during_polling():
    slept = []

    class SlowExports(FakePortkey):
        def __init__(self, rows_by_window, in_progress_rounds):
            super().__init__(rows_by_window)
            self._calls = {}
            self._threshold = in_progress_rounds

        def get_export(self, export_id):
            n = self._calls.get(export_id, 0)
            self._calls[export_id] = n + 1
            status = STATUS_SUCCESS if n >= self._threshold else STATUS_IN_PROGRESS
            return PortkeyExport(export_id=export_id, total=None, status=status)

    clock = FakeClock()

    def slow_sleep(seconds):
        slept.append(seconds)
        clock.advance(RENEW_INTERVAL_SECONDS + 1.0)  # each poll round outlives the interval

    pk = SlowExports({(WINDOW_START, WINDOW_END): [_portkey_row("r1")]}, in_progress_rounds=3)
    mg = FakeMeterGraph(_acquired())

    outcome = _run(mg, pk, [], sleep=slow_sleep, clock=clock)

    assert outcome.status == "completed"
    assert len(slept) == 3                       # slept once per in-progress round
    assert mg.renewed >= 3                        # renewed on each slow poll round


def test_lease_renewed_during_a_long_download_on_time_not_per_chunk():
    # A long download fires on_progress once per 64 KiB chunk. Renewal is time-based:
    # it must renew periodically as wall time passes, never once per chunk.
    chunks = 2000

    class ChunkyDownload(FakePortkey):
        def __init__(self, rows_by_window, clock):
            super().__init__(rows_by_window)
            self._clock = clock

        def download_to(self, export_id, dest_path, *, on_progress=None):
            rows = self._rows.get(self._win_by_id[export_id], [])
            with open(dest_path, "w") as f:
                for r in rows:
                    f.write(json.dumps(r) + "\n")
            if on_progress is not None:
                for _ in range(chunks):
                    self._clock.advance(5.0)  # 5s per chunk -> a 10,000s download
                    on_progress()
            return len(rows)

    clock = FakeClock()
    pk = ChunkyDownload({(WINDOW_START, WINDOW_END): [_portkey_row("r1")]}, clock)
    mg = FakeMeterGraph(_acquired())

    outcome = _run(mg, pk, [], clock=clock, renew_interval_seconds=RENEW_INTERVAL_SECONDS)

    assert outcome.status == "completed"
    # 10,000s of download / 300s cadence ~= 33 renewals: far below one-per-chunk (2000)
    # yet frequent enough that the 900s lease never lapses.
    assert mg.renewed < chunks // 10           # no flooding
    assert mg.renewed >= 10000 // int(LEASE_SECONDS)  # comfortably renewed within the lease


def test_lease_renewed_periodically_during_a_long_row_by_row_upload():
    # push_file issues one request per row; a 50k-row push could outlive the lease.
    # The orchestrator renews on a time cadence during the push, not per row.
    rows = [_portkey_row(f"r{i}") for i in range(200)]
    clock = FakeClock()

    def slow_push(path, token, base_url=None, *, on_progress=None):
        n = 0
        for line in open(path).read().splitlines():
            if not line.strip():
                continue
            n += 1
            clock.advance(50.0)  # 50s per row -> 10,000s upload
            if on_progress is not None:
                on_progress()
        return (n, 0)

    pk = FakePortkey({(WINDOW_START, WINDOW_END): rows})
    mg = FakeMeterGraph(_acquired())

    outcome = _run(mg, pk, [], push=slow_push, clock=clock, renew_interval_seconds=RENEW_INTERVAL_SECONDS)

    assert outcome.status == "completed"
    assert mg.renewed < len(rows) // 4         # not one renewal per row (no flooding)
    assert mg.renewed >= 10000 // int(LEASE_SECONDS)  # renewed repeatedly within the lease


# -- lease lost ------------------------------------------------------------


def test_lease_lost_during_renew_does_not_abandon_and_exits_nonzero():
    rows = [_portkey_row("r1")]
    pk = FakePortkey({(WINDOW_START, WINDOW_END): rows})
    mg = FakeMeterGraph(_acquired())
    mg.renew_error = LeaseLostError("expired")

    outcome = _run(mg, pk, [])

    assert outcome.status == "failed"
    assert outcome.exit_code == 1
    assert mg.completed == []
    assert mg.abandoned == []       # lease already gone; no DELETE attempted


def test_lease_lost_during_complete_does_not_abandon_and_exits_nonzero():
    rows = [_portkey_row("r1")]
    pk = FakePortkey({(WINDOW_START, WINDOW_END): rows})
    mg = FakeMeterGraph(_acquired())
    mg.complete_error = LeaseLostError("expired at the finish line")

    outcome = _run(mg, pk, [])

    assert outcome.status == "failed"
    assert outcome.exit_code == 1
    assert mg.completed == []
    assert mg.abandoned == []       # lease already gone; no DELETE attempted


def test_lease_lost_raised_from_the_upload_phase_does_not_abandon():
    # A renew invoked from push's on_progress can discover the lease is gone; the
    # LeaseLostError must surface as a no-abandon failure just like a direct renew.
    pk = FakePortkey({(WINDOW_START, WINDOW_END): [_portkey_row("r1")]})
    mg = FakeMeterGraph(_acquired())

    def push_loses_lease(path, token, base_url=None, *, on_progress=None):
        raise LeaseLostError("lease expired mid-upload")

    outcome = _run(mg, pk, [], push=push_loses_lease)

    assert outcome.status == "failed"
    assert outcome.exit_code == 1
    assert mg.completed == []
    assert mg.abandoned == []                 # lease gone -> no DELETE
    assert "exp-1" in pk.cancelled            # best-effort cancel still runs


# -- ordering --------------------------------------------------------------


def test_complete_fires_only_after_every_push_succeeded():
    # Split into ten; complete must be the very last lifecycle event, after all
    # ten pushes have reported zero failures.
    full = (WINDOW_START, WINDOW_END)
    pk = FakePortkey({}, totals={full: VOLUME_SPLIT_THRESHOLD + 1})
    original_create = pk.create_export

    def seeding_create(*, window_start, window_end):
        pk._rows.setdefault((window_start, window_end), [_portkey_row(f"r-{window_start}")])
        return original_create(window_start=window_start, window_end=window_end)

    pk.create_export = seeding_create

    mg = FakeMeterGraph(_acquired())
    order = []

    def logging_push(path, token, base_url=None, *, on_progress=None):
        rows = [json.loads(line) for line in open(path).read().splitlines() if line.strip()]
        order.append("push")
        return (len(rows), 0)

    original_complete = mg.complete

    def logging_complete(lease_id):
        order.append("complete")
        original_complete(lease_id)

    mg.complete = logging_complete

    outcome = _run(mg, pk, [], push=logging_push)

    assert outcome.status == "completed"
    assert order.count("push") == 10
    assert order[-1] == "complete"                 # complete is strictly last
    assert order.index("complete") == len(order) - 1
