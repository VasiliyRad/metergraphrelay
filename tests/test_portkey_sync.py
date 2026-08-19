"""Tests for the CLI-independent Portkey sync orchestrator (Task 5).

These drive ``run_portkey_sync`` against in-memory fakes for the MeterGraph
import-sync client, the Portkey Logs Export client, and the push function, so the
whole acquire -> plan -> start -> poll(+renew) -> download -> normalize -> push ->
complete lifecycle is exercised without any network.
"""

import json

import pytest

from metergraphrelay.metergraph_sync import (
    AcquiredLease,
    AcquireResult,
    LeaseLostError,
    MeterGraphSyncError,
)
from metergraphrelay.providers.portkey_export import (
    STATUS_DRAFT,
    STATUS_FAILED,
    STATUS_IN_PROGRESS,
    STATUS_SUCCESS,
    PortkeyExport,
    PortkeyExportError,
)
from metergraphrelay.portkey_sync import (
    DOWNLOAD_RENEW_EVERY_CHUNKS,
    VOLUME_SPLIT_THRESHOLD,
    run_portkey_sync,
)

WINDOW_START = "2026-08-19T00:00:00+00:00"
WINDOW_END = "2026-08-19T01:00:00+00:00"


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
        self.acquire_kwargs = None
        self.renewed = 0
        self.completed = []
        self.abandoned = []
        self.renew_error = None
        self.complete_error = None
        self.events = []  # ordered log of ("renew"|"complete"|"abandon", ...)

    def acquire(self, **kwargs):
        self.acquire_kwargs = kwargs
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
    def fake_push(path, token, base_url=None):
        rows = [json.loads(line) for line in open(path).read().splitlines() if line.strip()]
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
    assert mg.renewed >= 1                      # renewed during long phases
    # Pushed rows carry the server-dedup fields:
    pushed_rows = [r for p in pushes for r in p["rows"]]
    assert {r["import_event_id"] for r in pushed_rows} == {"r1", "r2"}
    assert all(r["import_source"] == "portkey" for r in pushed_rows)
    assert all(r["import_source_scope"] == "ws-acme" for r in pushed_rows)
    assert pushes[0]["token"] == "tok-123"
    assert outcome.pushed == 2


# -- volume split ----------------------------------------------------------


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


# -- failure handling ------------------------------------------------------


def test_push_failure_abandons_lease_and_exits_nonzero_without_completing():
    rows = [_portkey_row("r1")]
    pk = FakePortkey({(WINDOW_START, WINDOW_END): rows})
    mg = FakeMeterGraph(_acquired())

    def failing_push(path, token, base_url=None):
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


# -- lease renewal ---------------------------------------------------------


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

    pk = SlowExports({(WINDOW_START, WINDOW_END): [_portkey_row("r1")]}, in_progress_rounds=3)
    mg = FakeMeterGraph(_acquired())

    outcome = _run(mg, pk, [], sleep=lambda s: slept.append(s))

    assert outcome.status == "completed"
    assert len(slept) == 3                       # slept once per in-progress round
    assert mg.renewed >= 3                        # renewed on each poll round (plus phases)


def test_lease_renewed_during_long_download_without_flooding():
    # A long download fires on_progress once per 64 KiB chunk. The orchestrator must
    # renew on a throttled cadence, NOT once per chunk.
    callbacks = DOWNLOAD_RENEW_EVERY_CHUNKS * 20  # simulate a large, chunky download

    class ChunkyDownload(FakePortkey):
        def download_to(self, export_id, dest_path, *, on_progress=None):
            rows = self._rows.get(self._win_by_id[export_id], [])
            with open(dest_path, "w") as f:
                for r in rows:
                    f.write(json.dumps(r) + "\n")
            if on_progress is not None:
                for _ in range(callbacks):
                    on_progress()
            return len(rows)

    pk = ChunkyDownload({(WINDOW_START, WINDOW_END): [_portkey_row("r1")]})
    mg = FakeMeterGraph(_acquired())

    outcome = _run(mg, pk, [])

    assert outcome.status == "completed"
    # Download contributed at least the throttled number of renewals ...
    assert mg.renewed >= callbacks // DOWNLOAD_RENEW_EVERY_CHUNKS
    # ... but nowhere near one-per-chunk (proves the throttle; small fixed slack for
    # the explicit phase renewals around download/upload).
    assert mg.renewed <= callbacks // DOWNLOAD_RENEW_EVERY_CHUNKS + 10


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

    def logging_push(path, token, base_url=None):
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
