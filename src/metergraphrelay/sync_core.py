"""Pieces shared by every server-coordinated sync loop.

Portkey's cron mode and the cursor-paged providers (Langfuse, Braintrust,
Phoenix) drive the same lease contract against the metergraph import-sync
server, so the outcome type, the time-based renewer and its cadence live
here rather than in either provider's module.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

# Renew at most this often. Comfortably inside the server's 15-minute (900s) lease
# so a renewal always lands before the lease could lapse, while never renewing more
# than once per interval however frequently progress ticks arrive.
RENEW_INTERVAL_SECONDS = 300.0


@dataclass(frozen=True)
class SyncOutcome:
    status: str          # "completed" | "caught_up" | "busy" | "failed"
    detail: str          # human-facing summary line
    exit_code: int       # 0 for completed/caught_up/busy; 1 for failed
    pushed: int = 0
    failed: int = 0
    skipped: int = 0


class LeaseRenewer:
    """Renew a lease on a time-based cadence, driven by frequent progress ticks.

    ``tick()`` is safe to call very often (per row, per 64 KiB download chunk); it
    renews only when at least ``interval`` seconds of monotonic time have elapsed
    since the last renewal — so it neither floods a fast phase nor lets a slow phase
    outlive the lease. ``force()`` renews unconditionally (used at phase boundaries)
    and resets the timer. A ``LeaseLostError`` (or any error) raised by the
    underlying renew propagates to the caller, which treats a lost lease as a
    handled, no-abandon failure.
    """

    def __init__(self, renew: Callable[[], object], *, clock: Callable[[], float], interval: float) -> None:
        self._renew = renew
        self._clock = clock
        self._interval = interval
        self._last = clock()

    def tick(self) -> None:
        now = self._clock()
        if now - self._last >= self._interval:
            self._renew()
            self._last = self._clock()

    def force(self) -> None:
        self._renew()
        self._last = self._clock()


