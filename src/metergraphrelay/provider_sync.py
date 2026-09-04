"""CLI-independent sync orchestrator for the cursor-paged pull providers.

Langfuse, Braintrust and Phoenix all answer a bounded time-range query with
cursor pagination, so one loop serves all three:

    acquire -> pull(window, +ImportContext, +renew) -> push(+renew) -> complete

The MeterGraph import-sync server owns every piece of resume state: it picks
the logical window (at most one hour, with a 5-minute overlap on the previous
checkpoint), issues a 15-minute renewable lease, and advances the checkpoint
only when the run completes the lease it was issued. The relay keeps **no
local checkpoint** and stages the pulled rows only under a
``tempfile.TemporaryDirectory`` removed at the end of the run. Overlap
re-pulled rows carry ``import_source`` / ``import_source_scope`` /
``import_event_id``, which the server deduplicates on, so overlap never
double-counts.

Exit behaviour matches ``sync portkey``: ``busy`` and ``caught_up`` are clean
no-op exits (0); a handled failure releases the lease and exits nonzero; a
lease lost mid-run exits nonzero without a release (there is nothing left to
release); a process crash performs no cleanup and the server's lease expiry
is the backstop. A window advances only when every row uploads cleanly: a
failed upload, a row the provider could not normalize, or a row without a
valid import identity all release the lease so the same window is retried
next run. ``allow_skipped`` opts a run into advancing past rows the
provider skipped as malformed, for a window that would otherwise stay
pending forever; the skipped count is still reported.

Unlike Portkey there is no draft/poll/split phase. A provider page is fetched
on demand, so a large window costs more pages rather than a bigger export, and
the per-row ``on_progress`` hook keeps the lease renewed however long that
takes.
"""
from __future__ import annotations

import os
import tempfile
import time
from typing import Callable, Protocol

from .metergraph_sync import LeaseLostError, MeterGraphSyncError
from .portkey_sync import RENEW_INTERVAL_SECONDS, SyncOutcome, _LeaseRenewer
from .import_identity import ImportContext, ImportIdentityError
from .push import push_file

SYNC_SOURCES = ("langfuse", "braintrust", "phoenix")
# Sync never caps rows: the server bounds the window in time, and every row in
# it must land for the checkpoint to advance.
UNBOUNDED_COUNT = 1_000_000_000


class PullWindow(Protocol):
    """One provider's bounded pull, as the orchestrator calls it."""

    def __call__(
        self,
        *,
        window_start: str,
        window_end: str,
        output_path: str,
        import_context: ImportContext,
        on_progress: Callable[[], None],
    ) -> tuple[int, int]: ...


def run_pull_sync(
    *,
    mg_client,
    source: str,
    source_scope: str,
    pull_window: PullWindow,
    initial_since: str | None,
    max_window_seconds: int,
    push_token: str,
    ingest_base_url: str | None,
    provider_errors: tuple[type[Exception], ...] = (),
    allow_skipped: bool = False,
    work_dir: str | None = None,
    clock: Callable[[], float] = time.monotonic,
    renew_interval_seconds: float = RENEW_INTERVAL_SECONDS,
    push: Callable[..., tuple[int, int]] = push_file,
) -> SyncOutcome:
    if source not in SYNC_SOURCES:
        raise ValueError(f"unsupported sync source: {source!r}")
    # Acquire is the one step before a lease exists: any failure here must NOT
    # attempt a release — there is nothing to release.
    try:
        acquire = mg_client.acquire(
            source=source,
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
    ctx = ImportContext(source=source, source_scope=source_scope)

    def renew() -> None:
        mg_client.renew(lease.lease_id)

    renewer = _LeaseRenewer(renew, clock=clock, interval=renew_interval_seconds)
    try:
        with tempfile.TemporaryDirectory(dir=work_dir) as staging:
            rows_path = os.path.join(staging, "rows.jsonl")
            imported, skipped = pull_window(
                window_start=lease.window_start,
                window_end=lease.window_end,
                output_path=rows_path,
                import_context=ctx,
                on_progress=renewer.tick,
            )
            renewer.force()  # pull done — force before the row-by-row upload
            if skipped and not allow_skipped:
                # In sync mode there is no export file to recover a skipped row
                # from: once the checkpoint advances it is gone. Leave the
                # window pending unless the caller accepted that.
                _safe_abandon(mg_client, lease.lease_id)
                return SyncOutcome(
                    "failed",
                    f"{skipped} row(s) in window {lease.window_start}.."
                    f"{lease.window_end} could not be normalized (see warnings "
                    "above); lease released and the window left pending. Fix the "
                    "rows, or pass --allow-skipped to advance past them.",
                    1, pushed=0, failed=0, skipped=skipped,
                )
            pushed = failed = 0
            if imported:
                pushed, failed = push(
                    rows_path, push_token, base_url=ingest_base_url,
                    on_progress=renewer.tick,
                )
            if failed:
                # Do NOT complete: the untouched server checkpoint is retried on
                # the next run, so nothing is silently dropped.
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
        # Already gone (expired/stolen), including when discovered by a renew
        # fired from a progress tick. No DELETE: there is nothing to release.
        return SyncOutcome(
            "failed", f"Lease lost mid-run ({exc}); relying on server lease expiry.", 1
        )
    except (MeterGraphSyncError, OSError, ImportIdentityError, *provider_errors) as exc:
        _safe_abandon(mg_client, lease.lease_id)
        return SyncOutcome("failed", f"Sync failed: {exc}; lease released.", 1)
    except Exception:
        # Anything unforeseen still propagates as a traceback, but never with
        # the lease held: the next run must not sit on "busy" for 15 minutes
        # because of a bug here.
        _safe_abandon(mg_client, lease.lease_id)
        raise


def _safe_abandon(mg_client, lease_id: str) -> None:
    """Release the lease, swallowing release errors so they never mask a primary failure."""
    try:
        mg_client.abandon(lease_id)
    except MeterGraphSyncError:
        pass
