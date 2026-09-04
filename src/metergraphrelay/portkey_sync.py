"""CLI-independent Portkey API-cron sync orchestrator.

Ties the leaf modules together into one resumable, idempotent run:

    acquire -> plan (from the draft total) -> start -> poll(+renew)
            -> download -> normalize(+ImportContext) -> push -> complete

All resume/checkpoint/overlap/lease state lives on the MeterGraph import-sync
server; the relay keeps **no local checkpoint** and stages downloaded/normalized
data only under a ``tempfile.TemporaryDirectory`` that is removed at the end of the
run. ``busy`` and ``caught_up`` are clean no-op exits (exit 0). A handled failure
releases the lease (``DELETE``) and exits nonzero, except when the lease was lost
during renew/complete (already gone -> exit nonzero without an abandon) or when
acquire itself failed (no lease exists to release). A process crash performs no
cleanup; the server's lease expiry is the backstop.

The server issues a 15-minute lease, but a single window's download and (one HTTP
request per row) upload can each run for many minutes. Renewal is therefore
**time-based**: an injected monotonic clock drives a :class:`_LeaseRenewer` that
renews at most once per :data:`RENEW_INTERVAL_SECONDS` no matter how often progress
callbacks fire (so a fast, huge transfer never floods renewals) and renews as soon
as that interval has elapsed on a slow one (so the lease never lapses). Phase
boundaries force an unconditional renewal.
"""

from __future__ import annotations

import os
import tempfile
import time
from typing import Callable

from .metergraph_sync import LeaseLostError, MeterGraphSyncError
from .sync_core import RENEW_INTERVAL_SECONDS, LeaseRenewer, SyncOutcome
from .providers.portkey import (
    ImportContext,
    PortkeyConversionError,
    convert_portkey_export,
)
from .providers.portkey_export import PAGE_SIZE_MAX, PortkeyExportError
from .push import push_file
from .window import TimeWindow, split_window

# Kept as aliases so existing imports keep working; the definitions moved to
# sync_core.py when the cursor-paged providers gained the same loop.
_LeaseRenewer = LeaseRenewer
__all__ = ["RENEW_INTERVAL_SECONDS", "SyncOutcome", "_LeaseRenewer", "run_portkey_sync"]

PORTKEY_SOURCE = "portkey"
# Decided from the draft's ``total``: <= threshold uses the hourly draft as-is;
# strictly greater triggers the one-shot 10-way split. (total == threshold does
# NOT split.) Derived from ``PAGE_SIZE_MAX`` — create_export fetches exactly one
# page of that many rows, so a window whose total exceeds it cannot fit in a single
# page and must split. Keeping this a single source of truth makes it impossible
# for the threshold to silently drift above the page size and drop rows.
VOLUME_SPLIT_THRESHOLD = PAGE_SIZE_MAX
POLL_INTERVAL_SECONDS = 15.0
MAX_POLL_SECONDS = 3300.0  # 55 min safety cap; renewal keeps the lease alive within it


def run_portkey_sync(
    *,
    mg_client,
    pk_client,
    source_scope: str,
    initial_since: str | None,
    max_window_seconds: int,
    push_token: str,
    ingest_base_url: str | None,
    work_dir: str | None = None,
    sleep: Callable[[float], object] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
    max_poll_seconds: float = MAX_POLL_SECONDS,
    renew_interval_seconds: float = RENEW_INTERVAL_SECONDS,
    push: Callable[..., tuple[int, int]] = push_file,
) -> SyncOutcome:
    # Acquire is the one step before a lease exists: any failure here (network error,
    # unknown status, or an "acquired" response with no lease) must NOT attempt an
    # abandon — there is nothing to release.
    try:
        acquire = mg_client.acquire(
            source=PORTKEY_SOURCE,
            source_scope=source_scope,
            initial_since=initial_since,
            max_window_seconds=max_window_seconds,
        )
    except MeterGraphSyncError as exc:
        return SyncOutcome("failed", f"Could not acquire a sync lease: {exc}", 1)
    if acquire.status == "caught_up":
        return SyncOutcome("caught_up", "Already caught up — nothing to import.", 0)
    if acquire.status == "busy":
        return SyncOutcome(
            "busy",
            f"Another sync holds the lease; retry at {acquire.retry_at}.",
            0,
        )
    if acquire.status != "acquired" or acquire.lease is None:
        return SyncOutcome(
            "failed",
            f"Unexpected acquire response (status={acquire.status!r}, "
            f"lease={'present' if acquire.lease else 'missing'}); no lease to release.",
            1,
        )

    lease = acquire.lease
    ctx = ImportContext(source=PORTKEY_SOURCE, source_scope=source_scope)
    created_export_ids: list[str] = []  # every draft this run created, for best-effort cancel

    def renew() -> None:
        mg_client.renew(lease.lease_id)

    renewer = LeaseRenewer(renew, clock=clock, interval=renew_interval_seconds)
    try:
        with tempfile.TemporaryDirectory(dir=work_dir) as staging:
            # 1) Plan from the draft total(s) — decides the split BEFORE starting
            #    anything. create/cancel can each be slow, so renew as we go.
            export_ids = _plan_exports(pk_client, lease, created_export_ids, renewer)
            # 2) Start every planned export (up to ten), renewing between starts,
            #    then poll them all together, renewing across the poll loop.
            for export_id in export_ids:
                pk_client.start_export(export_id)
                renewer.tick()
            renewer.force()  # submission finished — force a renewal entering the poll phase
            _poll_all(
                export_ids, pk_client, renewer,
                sleep, poll_interval_seconds, max_poll_seconds,
            )
            # 3) Download -> normalize(+ImportContext) -> push, renewing throughout.
            renewer.force()  # entering the download/normalize/upload phase
            pushed = failed = skipped = 0
            for i, export_id in enumerate(export_ids):
                raw = os.path.join(staging, f"raw-{i}.jsonl")
                converted_path = os.path.join(staging, f"converted-{i}.jsonl")
                pk_client.download_to(export_id, raw, on_progress=renewer.tick)
                renewer.force()  # download done — force before normalize
                _, sk = convert_portkey_export(
                    raw, converted_path, import_context=ctx, on_progress=renewer.tick
                )
                _discard_file(raw)  # raw fully consumed — free it before the upload
                skipped += sk
                renewer.force()  # normalize done — force before the row-by-row upload
                s, f = push(
                    converted_path, push_token, base_url=ingest_base_url,
                    on_progress=renewer.tick,
                )
                _discard_file(converted_path)  # pushed — free it before the next sub-window
                pushed += s
                failed += f
            if failed:
                # A push reported failed rows: release the lease (do NOT complete) so
                # the untouched server checkpoint is retried on the next run.
                _safe_abandon(mg_client, lease.lease_id)
                return SyncOutcome(
                    "failed",
                    f"{failed} row(s) failed to upload; lease released, will retry next run.",
                    1, pushed=pushed, failed=failed, skipped=skipped,
                )
            mg_client.complete(lease.lease_id)  # complete ONLY after every upload succeeded
            return SyncOutcome(
                "completed",
                f"Imported window {lease.window_start}..{lease.window_end}: "
                f"pushed {pushed} row(s), skipped {skipped}, {failed} failed.",
                0, pushed=pushed, failed=failed, skipped=skipped,
            )
    except LeaseLostError as exc:
        # The lease is already gone (expired/stolen) — including when discovered by a
        # renew fired from a progress callback. Best-effort cancel the exports this
        # run created, but do NOT attempt a DELETE: there is nothing to release.
        _best_effort_cancel(pk_client, created_export_ids)
        return SyncOutcome(
            "failed", f"Lease lost mid-run ({exc}); relying on server lease expiry.", 1
        )
    except (PortkeyExportError, PortkeyConversionError, MeterGraphSyncError, OSError) as exc:
        # Handled primary failure: best-effort cancel every possibly non-terminal
        # export, then release the lease — neither may mask the primary error.
        _best_effort_cancel(pk_client, created_export_ids)
        _safe_abandon(mg_client, lease.lease_id)
        return SyncOutcome("failed", f"Sync failed: {exc}; lease released.", 1)


def _plan_exports(pk_client, lease, created_export_ids: list[str], renewer) -> list[str]:
    """Create the hourly draft; its ``total`` decides whether to split. No start/poll here.

    ``total > threshold`` cancels the still-unstarted hourly draft and creates
    exactly 10 overlapping sub-window drafts. If any sub-window draft is itself
    still oversized, reject with a clear error — the MVP never splits recursively.
    Each create/cancel can be slow, so the lease is renewed on a time cadence
    between them.
    """
    hourly = pk_client.create_export(
        window_start=lease.window_start, window_end=lease.window_end
    )
    created_export_ids.append(hourly.export_id)
    renewer.tick()
    if hourly.total is None or hourly.total <= VOLUME_SPLIT_THRESHOLD:
        return [hourly.export_id]

    # Oversized: cancel the still-unstarted hourly draft, then split into exactly 10.
    pk_client.cancel_export(hourly.export_id)
    created_export_ids.remove(hourly.export_id)
    renewer.tick()
    # split_window validates the server-provided bounds (aware, end-after-start). A
    # bad/reversed/naive window raises ValueError; wrap it as PortkeyExportError right
    # at the planning boundary so it flows through normal handled-failure cleanup
    # (best-effort cancel + abandon + exit 1) instead of leaking as a traceback — and
    # without globally swallowing unrelated ValueErrors elsewhere in the run.
    try:
        sub_windows = split_window(
            TimeWindow(start=lease.window_start, end=lease.window_end)
        )
    except ValueError as exc:
        raise PortkeyExportError(
            f"cannot split window {lease.window_start}..{lease.window_end}: {exc}"
        ) from exc
    export_ids: list[str] = []
    for w in sub_windows:
        draft = pk_client.create_export(window_start=w.start, window_end=w.end)
        created_export_ids.append(draft.export_id)
        renewer.tick()
        if draft.total is not None and draft.total > VOLUME_SPLIT_THRESHOLD:
            raise PortkeyExportError(
                f"sub-window {w.start}..{w.end} still exceeds {VOLUME_SPLIT_THRESHOLD} "
                f"records ({draft.total}); MVP does not split recursively"
            )
        export_ids.append(draft.export_id)
    return export_ids


def _poll_all(export_ids, pk_client, renewer, sleep, poll_interval, max_poll_seconds) -> None:
    """Poll every export to a terminal state, renewing the lease on a time cadence.

    Elapsed time is tracked by summing the poll interval (no wall clock) and bounded
    by ``max_poll_seconds``. Any export that reaches a terminal state other than
    success fails the run with a clear, export-naming error.
    """
    elapsed = 0.0
    states = {eid: pk_client.get_export(eid) for eid in export_ids}
    while not all(e.is_terminal for e in states.values()):
        sleep(poll_interval)
        elapsed += poll_interval
        renewer.tick()  # keep the lease alive across the whole poll loop
        states = {
            eid: (e if e.is_terminal else pk_client.get_export(eid))
            for eid, e in states.items()
        }
        if elapsed >= max_poll_seconds:
            raise PortkeyExportError(
                f"Portkey export polling exceeded {max_poll_seconds}s safety cap"
            )
    failures = [eid for eid, e in states.items() if not e.is_success]
    if failures:
        raise PortkeyExportError(
            f"Portkey export(s) did not succeed: {', '.join(failures)}"
        )


def _best_effort_cancel(pk_client, export_ids) -> None:
    """Cancel any created, possibly non-terminal exports without masking the primary error."""
    for export_id in export_ids:
        try:
            pk_client.cancel_export(export_id)
        except (PortkeyExportError, OSError):
            pass


def _discard_file(path: str) -> None:
    """Best-effort delete of a consumed staging file so per-sub-window data never piles up.

    Deletion is eager (right after a file is consumed) to bound peak disk to a single
    sub-window's raw + converted footprint. Any error is swallowed: the enclosing
    ``TemporaryDirectory`` removes whatever remains at the end of the run, so a failed
    early delete must never turn an otherwise-successful sync into a failure.
    """
    try:
        os.remove(path)
    except OSError:
        pass


def _safe_abandon(mg_client, lease_id: str) -> None:
    """Release the lease, swallowing release errors so they never mask a primary failure.

    ``abandon`` already treats an already-gone lease as a no-op; this additionally
    swallows a transient release error — the server's lease expiry is the backstop.
    """
    try:
        mg_client.abandon(lease_id)
    except MeterGraphSyncError:
        pass
