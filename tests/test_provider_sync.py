"""Tests for the CLI-independent sync orchestrator shared by Langfuse, Braintrust
and Phoenix.

``run_pull_sync`` is driven against an in-memory fake of the MeterGraph
import-sync client, a fake bounded pull, and a fake push, so the whole
acquire -> pull(+renew) -> push(+renew) -> complete lifecycle is exercised
without any network. The pull is handed the server's window verbatim and an
ImportContext naming the source and scope, which is what makes overlap
re-pulls deduplicate server-side.
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
from metergraphrelay.portkey_sync import RENEW_INTERVAL_SECONDS
from metergraphrelay.provider_sync import SYNC_SOURCES, run_pull_sync
from metergraphrelay.import_identity import ImportContext, ImportIdentityError

WINDOW_START = "2026-08-19T00:00:00+00:00"
WINDOW_END = "2026-08-19T01:00:00+00:00"


class FakeClock:
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

    def acquire(self, **kwargs):
        self.acquire_kwargs = kwargs
        if self.acquire_error:
            raise self.acquire_error
        return self._acquire

    def renew(self, lease_id):
        if self.renew_error:
            raise self.renew_error
        self.renewed += 1
        return "2026-08-19T00:30:00+00:00"

    def complete(self, lease_id):
        if self.complete_error:
            raise self.complete_error
        self.completed.append(lease_id)

    def abandon(self, lease_id):
        self.abandoned.append(lease_id)


class ProviderBoom(Exception):
    pass


class FakePull:
    """Writes ``rows`` to the output path, ticking progress per row, or raises."""

    def __init__(self, rows, *, error=None, skipped=0, ticks_per_row=1):
        self.rows = rows
        self.error = error
        self.skipped = skipped
        self.ticks_per_row = ticks_per_row
        self.calls = []

    def __call__(self, *, window_start, window_end, output_path, import_context, on_progress):
        self.calls.append(
            {"window_start": window_start, "window_end": window_end,
             "output_path": output_path, "import_context": import_context}
        )
        if self.error:
            raise self.error
        with open(output_path, "w") as f:
            for row in self.rows:
                f.write(json.dumps(row) + "\n")
                for _ in range(self.ticks_per_row):
                    on_progress()
        return len(self.rows), self.skipped


def _run(mg, pull, pushes, *, fail_rows=0, **overrides):
    def fake_push(path, token, base_url=None, *, on_progress=None):
        rows = [json.loads(line) for line in open(path).read().splitlines() if line.strip()]
        for _ in rows:
            if on_progress is not None:
                on_progress()
        pushes.append({"token": token, "base_url": base_url, "rows": rows, "path": path})
        return (len(rows) - fail_rows, fail_rows)

    kwargs = dict(
        mg_client=mg, source="langfuse", source_scope="pk-lf-1",
        pull_window=pull, initial_since="2026-08-01T00:00:00+00:00",
        max_window_seconds=3600, push_token="tok-123", ingest_base_url=None,
        provider_errors=(ProviderBoom,), push=fake_push,
    )
    kwargs.update(overrides)
    return run_pull_sync(**kwargs)


def test_sync_sources_are_the_three_cursor_paged_providers():
    assert set(SYNC_SOURCES) == {"langfuse", "braintrust", "phoenix"}


def test_rejects_a_source_outside_the_sync_set():
    with pytest.raises(ValueError, match="unsupported sync source"):
        _run(FakeMeterGraph(_acquired()), FakePull([]), [], source="openai")


@pytest.mark.parametrize("source", SYNC_SOURCES)
def test_happy_path_pulls_the_server_window_pushes_and_completes(source):
    mg = FakeMeterGraph(_acquired())
    pull = FakePull([{"request_id": "a"}, {"request_id": "b"}])
    pushes = []

    outcome = _run(mg, pull, pushes, source=source, source_scope="scope-x")

    assert outcome.status == "completed"
    assert outcome.exit_code == 0
    assert (outcome.pushed, outcome.failed, outcome.skipped) == (2, 0, 0)
    assert mg.acquire_kwargs == {
        "source": source, "source_scope": "scope-x",
        "initial_since": "2026-08-01T00:00:00+00:00", "max_window_seconds": 3600,
    }
    # The pull got the server's window verbatim and an ImportContext naming the
    # source/scope, so every row carries dedup identity.
    assert pull.calls[0]["window_start"] == WINDOW_START
    assert pull.calls[0]["window_end"] == WINDOW_END
    assert pull.calls[0]["import_context"] == ImportContext(source=source, source_scope="scope-x")
    assert [r["request_id"] for r in pushes[0]["rows"]] == ["a", "b"]
    assert pushes[0]["token"] == "tok-123"
    assert mg.completed == ["lease-1"]
    assert mg.abandoned == []
    assert WINDOW_START in outcome.detail and "pushed 2" in outcome.detail


def test_staging_file_is_removed_after_the_run():
    mg = FakeMeterGraph(_acquired())
    pushes = []
    _run(mg, FakePull([{"request_id": "a"}]), pushes)
    assert not os.path.exists(pushes[0]["path"])
    assert not os.path.isdir(os.path.dirname(pushes[0]["path"]))


def test_empty_window_completes_without_pushing():
    mg = FakeMeterGraph(_acquired())
    pushes = []
    outcome = _run(mg, FakePull([]), pushes)
    assert outcome.status == "completed"
    assert pushes == []
    assert mg.completed == ["lease-1"]


def test_caught_up_and_busy_are_clean_noops():
    pushes = []
    mg = FakeMeterGraph(AcquireResult(status="caught_up"))
    pull = FakePull([{"request_id": "a"}])
    outcome = _run(mg, pull, pushes)
    assert (outcome.status, outcome.exit_code) == ("caught_up", 0)
    assert pull.calls == [] and pushes == [] and mg.completed == []

    mg = FakeMeterGraph(AcquireResult(status="busy", retry_at="2026-08-19T00:10:00+00:00"))
    outcome = _run(mg, pull, pushes)
    assert (outcome.status, outcome.exit_code) == ("busy", 0)
    assert "2026-08-19T00:10:00+00:00" in outcome.detail
    assert pull.calls == [] and mg.abandoned == []


def test_acquire_failure_does_not_attempt_a_release():
    mg = FakeMeterGraph(_acquired())
    mg.acquire_error = MeterGraphSyncError("boom")
    outcome = _run(mg, FakePull([]), [])
    assert (outcome.status, outcome.exit_code) == ("failed", 1)
    assert mg.abandoned == [] and mg.completed == []


def test_unexpected_acquire_shape_fails_without_release():
    mg = FakeMeterGraph(AcquireResult(status="acquired", lease=None))
    outcome = _run(mg, FakePull([]), [])
    assert (outcome.status, outcome.exit_code) == ("failed", 1)
    assert mg.abandoned == []


def test_failed_rows_release_the_lease_and_never_complete():
    mg = FakeMeterGraph(_acquired())
    pushes = []
    outcome = _run(mg, FakePull([{"request_id": "a"}, {"request_id": "b"}]), pushes, fail_rows=1)
    assert (outcome.status, outcome.exit_code) == ("failed", 1)
    assert (outcome.pushed, outcome.failed) == (1, 1)
    assert mg.completed == []
    assert mg.abandoned == ["lease-1"]
    assert "retry next run" in outcome.detail


def test_provider_error_releases_the_lease():
    mg = FakeMeterGraph(_acquired())
    outcome = _run(mg, FakePull([], error=ProviderBoom("api down")), [])
    assert (outcome.status, outcome.exit_code) == ("failed", 1)
    assert "api down" in outcome.detail
    assert mg.abandoned == ["lease-1"] and mg.completed == []


def test_unlisted_provider_error_propagates_but_still_releases_the_lease():
    mg = FakeMeterGraph(_acquired())
    with pytest.raises(ProviderBoom):
        _run(mg, FakePull([], error=ProviderBoom("api down")), [], provider_errors=())
    # A bug must not leave the next run sitting on "busy" for 15 minutes.
    assert mg.abandoned == ["lease-1"]
    assert mg.completed == []


def test_skipped_rows_leave_the_window_pending_by_default():
    mg = FakeMeterGraph(_acquired())
    pushes = []
    outcome = _run(mg, FakePull([{"request_id": "a"}], skipped=2), pushes)
    assert (outcome.status, outcome.exit_code) == ("failed", 1)
    assert outcome.skipped == 2 and outcome.pushed == 0
    # Nothing uploaded, nothing completed: the same window comes back next run.
    assert pushes == []
    assert mg.completed == []
    assert mg.abandoned == ["lease-1"]
    assert "--allow-skipped" in outcome.detail


def test_allow_skipped_completes_the_window_and_reports_the_count():
    mg = FakeMeterGraph(_acquired())
    pushes = []
    outcome = _run(mg, FakePull([{"request_id": "a"}], skipped=2), pushes, allow_skipped=True)
    assert outcome.status == "completed"
    assert (outcome.pushed, outcome.skipped) == (1, 2)
    assert mg.completed == ["lease-1"]
    assert "skipped 2" in outcome.detail


def test_invalid_import_identity_fails_the_window_before_upload():
    mg = FakeMeterGraph(_acquired())
    pushes = []
    outcome = _run(
        mg, FakePull([], error=ImportIdentityError("import_event_id must be a string")), pushes,
        provider_errors=(),
    )
    assert (outcome.status, outcome.exit_code) == ("failed", 1)
    assert pushes == [] and mg.completed == []
    assert mg.abandoned == ["lease-1"]
    assert "import_event_id" in outcome.detail


def test_complete_failure_releases_the_lease():
    mg = FakeMeterGraph(_acquired())
    mg.complete_error = MeterGraphSyncError("complete 500")
    outcome = _run(mg, FakePull([{"request_id": "a"}]), [])
    assert (outcome.status, outcome.exit_code) == ("failed", 1)
    assert mg.abandoned == ["lease-1"]


def test_lease_lost_on_complete_exits_nonzero_without_release():
    mg = FakeMeterGraph(_acquired())
    mg.complete_error = LeaseLostError("gone")
    outcome = _run(mg, FakePull([{"request_id": "a"}]), [])
    assert (outcome.status, outcome.exit_code) == ("failed", 1)
    assert mg.abandoned == []
    assert "Lease lost" in outcome.detail


def test_lease_lost_during_a_progress_renew_aborts_the_pull():
    mg = FakeMeterGraph(_acquired())
    mg.renew_error = LeaseLostError("stolen")
    clock = FakeClock()
    pushes = []

    class SlowPull(FakePull):
        def __call__(self, **kwargs):
            inner = kwargs["on_progress"]

            def ticking():
                clock.advance(RENEW_INTERVAL_SECONDS + 1)
                inner()
            kwargs["on_progress"] = ticking
            return super().__call__(**kwargs)

    outcome = _run(mg, SlowPull([{"request_id": "a"}, {"request_id": "b"}]), pushes, clock=clock)
    assert outcome.status == "failed"
    assert pushes == []
    assert mg.abandoned == [] and mg.completed == []


def test_renews_on_a_time_cadence_across_pull_and_push():
    mg = FakeMeterGraph(_acquired())
    clock = FakeClock()
    rows = [{"request_id": str(i)} for i in range(6)]

    class SlowPull(FakePull):
        def __call__(self, **kwargs):
            inner = kwargs["on_progress"]

            def ticking():
                clock.advance(100.0)  # 6 rows -> 600s of pulling
                inner()
            kwargs["on_progress"] = ticking
            return super().__call__(**kwargs)

    def slow_push(path, token, base_url=None, *, on_progress=None):
        n = 0
        for line in open(path):
            if line.strip():
                n += 1
                clock.advance(100.0)  # 6 rows -> another 600s
                on_progress()
        return (n, 0)

    outcome = _run(mg, SlowPull(rows), [], clock=clock, push=slow_push)
    assert outcome.status == "completed"
    # 600s of pull -> 2 cadence renewals, one forced at the phase boundary,
    # 600s of push -> 2 more. Never one per row.
    assert mg.renewed == 5
    assert mg.renewed < len(rows) * 2


def test_os_error_during_pull_releases_the_lease():
    mg = FakeMeterGraph(_acquired())
    outcome = _run(mg, FakePull([], error=OSError("disk full")), [])
    assert (outcome.status, outcome.exit_code) == ("failed", 1)
    assert mg.abandoned == ["lease-1"]


def test_release_error_never_masks_the_primary_failure():
    class Flaky(FakeMeterGraph):
        def abandon(self, lease_id):
            raise MeterGraphSyncError("release 500")

    mg = Flaky(_acquired())
    outcome = _run(mg, FakePull([], error=ProviderBoom("api down")), [])
    assert outcome.status == "failed"
    assert "api down" in outcome.detail
