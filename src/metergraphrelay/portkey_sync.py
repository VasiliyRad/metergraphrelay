"""CLI-independent Portkey API-cron sync orchestrator.

Ties the leaf modules together into one resumable, idempotent run:

    acquire -> plan (from the draft total) -> start -> poll(+renew)
            -> download -> normalize(+ImportContext) -> push -> complete

All resume/checkpoint/overlap/lease state lives on the MeterGraph import-sync
server; the relay keeps **no local checkpoint** and stages downloaded/normalized
data only under a ``tempfile.TemporaryDirectory`` that is removed at the end of the
run. ``busy`` and ``caught_up`` are clean no-op exits (exit 0). A handled failure
releases the lease (``DELETE``) and exits nonzero, except when the lease was lost
during renew/complete (already gone -> exit nonzero without an abandon). A process
crash performs no cleanup; the server's lease expiry is the backstop.
"""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass
from typing import Callable

from .metergraph_sync import LeaseLostError, MeterGraphSyncError
from .providers.portkey import (
    ImportContext,
    PortkeyConversionError,
    convert_portkey_export,
)
from .providers.portkey_export import PortkeyExportError
from .push import push_file
from .window import TimeWindow, split_window

PORTKEY_SOURCE = "portkey"
# Decided from the draft's ``total``: <= threshold uses the hourly draft as-is;
# strictly greater triggers the one-shot 10-way split. (total == threshold does
# NOT split.)
VOLUME_SPLIT_THRESHOLD = 50_000
POLL_INTERVAL_SECONDS = 15.0
MAX_POLL_SECONDS = 3300.0  # 55 min safety cap; renew keeps the lease alive within it
# ``download_to`` fires ``on_progress`` once per streamed chunk (64 KiB). Renewing
# per chunk would flood the server, so renew only once every N chunks -- a bounded,
# throttled cadence that still keeps a long download's lease alive.
DOWNLOAD_RENEW_EVERY_CHUNKS = 64


@dataclass(frozen=True)
class SyncOutcome:
    status: str          # "completed" | "caught_up" | "busy" | "failed"
    detail: str          # human-facing summary line
    exit_code: int       # 0 for completed/caught_up/busy; 1 for failed
    pushed: int = 0
    failed: int = 0
    skipped: int = 0


class _ThrottledRenew:
    """Renew a lease at most once per ``every`` progress ticks.

    Used as the ``on_progress`` callback for a streaming download so the lease is
    kept alive across a long transfer without issuing a renew for every 64 KiB
    chunk. A ``LeaseLostError`` from the underlying renew is allowed to propagate
    (the caller treats a lost lease as a handled, no-abandon failure).
    """

    def __init__(self, renew: Callable[[], object], *, every: int) -> None:
        self._renew = renew
        self._every = max(1, every)
        self._ticks = 0

    def __call__(self) -> None:
        self._ticks += 1
        if self._ticks >= self._every:
            self._ticks = 0
            self._renew()


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
    poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
    max_poll_seconds: float = MAX_POLL_SECONDS,
    push: Callable[..., tuple[int, int]] = push_file,
) -> SyncOutcome:
    acquire = mg_client.acquire(
        source=PORTKEY_SOURCE,
        source_scope=source_scope,
        initial_since=initial_since,
        max_window_seconds=max_window_seconds,
    )
    if acquire.status == "caught_up":
        return SyncOutcome("caught_up", "Already caught up — nothing to import.", 0)
    if acquire.status == "busy":
        return SyncOutcome(
            "busy",
            f"Another sync holds the lease; retry at {acquire.retry_at}.",
            0,
        )

    lease = acquire.lease
    ctx = ImportContext(source=PORTKEY_SOURCE, source_scope=source_scope)
    created_export_ids: list[str] = []  # every draft this run created, for best-effort cancel

    def renew() -> None:
        mg_client.renew(lease.lease_id)

    try:
        with tempfile.TemporaryDirectory(dir=work_dir) as staging:
            # 1) Plan from the draft total(s) — decides the split BEFORE starting anything.
            export_ids = _plan_exports(pk_client, lease, created_export_ids)
            # 2) Start every planned export, then poll them all together, renewing.
            for export_id in export_ids:
                pk_client.start_export(export_id)
            renew()  # submission finished — keep the lease alive entering the poll phase
            _poll_all(
                export_ids, pk_client, renew,
                sleep, poll_interval_seconds, max_poll_seconds,
            )
            # 3) Download -> normalize(+ImportContext) -> push, renewing throughout.
            on_progress = _ThrottledRenew(renew, every=DOWNLOAD_RENEW_EVERY_CHUNKS)
            pushed = failed = skipped = 0
            for i, export_id in enumerate(export_ids):
                raw = os.path.join(staging, f"raw-{i}.jsonl")
                converted_path = os.path.join(staging, f"converted-{i}.jsonl")
                pk_client.download_to(export_id, raw, on_progress=on_progress)
                renew()  # renew after the download, before normalize/upload
                _, sk = convert_portkey_export(raw, converted_path, import_context=ctx)
                skipped += sk
                s, f = push(converted_path, push_token, base_url=ingest_base_url)
                pushed += s
                failed += f
                renew()  # renew after each upload
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
        # The lease is already gone (expired/stolen). Best-effort cancel the exports
        # this run created, but do NOT attempt a DELETE — there is nothing to release.
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


def _plan_exports(pk_client, lease, created_export_ids: list[str]) -> list[str]:
    """Create the hourly draft; its ``total`` decides whether to split. No start/poll here.

    ``total > threshold`` cancels the still-unstarted hourly draft and creates
    exactly 10 overlapping sub-window drafts. If any sub-window draft is itself
    still oversized, reject with a clear error — the MVP never splits recursively.
    """
    hourly = pk_client.create_export(
        window_start=lease.window_start, window_end=lease.window_end
    )
    created_export_ids.append(hourly.export_id)
    if hourly.total is None or hourly.total <= VOLUME_SPLIT_THRESHOLD:
        return [hourly.export_id]

    # Oversized: cancel the still-unstarted hourly draft, then split into exactly 10.
    pk_client.cancel_export(hourly.export_id)
    created_export_ids.remove(hourly.export_id)
    export_ids: list[str] = []
    for w in split_window(TimeWindow(start=lease.window_start, end=lease.window_end)):
        draft = pk_client.create_export(window_start=w.start, window_end=w.end)
        created_export_ids.append(draft.export_id)
        if draft.total is not None and draft.total > VOLUME_SPLIT_THRESHOLD:
            raise PortkeyExportError(
                f"sub-window {w.start}..{w.end} still exceeds {VOLUME_SPLIT_THRESHOLD} "
                f"records ({draft.total}); MVP does not split recursively"
            )
        export_ids.append(draft.export_id)
    return export_ids


def _poll_all(export_ids, pk_client, renew, sleep, poll_interval, max_poll_seconds) -> None:
    """Poll every export to a terminal state, renewing the lease each round.

    Elapsed time is tracked by summing the poll interval (no wall clock) and bounded
    by ``max_poll_seconds``. Any export that reaches a terminal state other than
    success fails the run with a clear, export-naming error.
    """
    elapsed = 0.0
    states = {eid: pk_client.get_export(eid) for eid in export_ids}
    while not all(e.is_terminal for e in states.values()):
        sleep(poll_interval)
        elapsed += poll_interval
        renew()  # keep the lease alive across the whole poll loop
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


def _safe_abandon(mg_client, lease_id: str) -> None:
    """Release the lease, swallowing release errors so they never mask a primary failure.

    ``abandon`` already treats an already-gone lease as a no-op; this additionally
    swallows a transient release error — the server's lease expiry is the backstop.
    """
    try:
        mg_client.abandon(lease_id)
    except MeterGraphSyncError:
        pass
