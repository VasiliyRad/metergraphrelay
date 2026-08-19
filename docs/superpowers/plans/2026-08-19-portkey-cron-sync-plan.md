# Resumable Portkey Cron Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Portkey **API cron mode** to `metergraphrelay sync portkey` that pulls a fixed logical time window from the Portkey Logs Export API, normalizes it, and pushes it to MeterGraph — with acquire/renew/complete/abandon coordination done entirely through MeterGraph's `/v1/import-sync` server so runs are safely resumable and idempotent, while the existing manual `sync portkey EXPORT_FILE` workflow is preserved unchanged.

**Architecture:** New leaf modules composed bottom-up, each independently testable:
- `window.py` — pure window planning (`split_window`, the >50k volume split into 10 overlapping intervals).
- `metergraph_sync.py` — `MeterGraphSyncClient`, a stdlib-urllib client for the fully-specified `/v1/import-sync` contract (acquire/renew/complete/abandon/state).
- `providers/portkey_export.py` — `PortkeyExportClient`, a stdlib-urllib client for Portkey's async Logs Export API (submit/poll/download). **This is the only module whose wire format is not verifiable from inside this repo; it is quarantined here behind a typed interface and named constants (see the ASSUMPTION callout in Task 4).**
- `providers/portkey.py` — existing normalization, extended backward-compatibly with an optional `ImportContext` so API-mode rows carry `import_source`/`import_source_scope`/`import_event_id` for server-side dedup.
- `portkey_sync.py` — `run_portkey_sync`, the CLI-independent orchestrator that ties acquire → submit → poll(+renew) → maybe-split → download → normalize → push → complete, releasing the lease on any handled failure.
- `cli.py` — `export_file` positional made optional (`nargs="?"`); when omitted, dispatch to the new `_run_sync_portkey_api`.

The MeterGraph server owns all checkpoint/overlap/lease state; the relay keeps **no local checkpoint files**. `push.py` and the existing manual path are untouched.

**Tech Stack:** Python 3.10+ stdlib only (`urllib.request`, `tempfile`, `datetime`, `dataclasses`, `argparse`). No new runtime dependency. `pytest` for tests.

**Spec:** This plan is self-contained — the approved server contract and relay behavior are embedded verbatim in **§ Approved Contract** below (the plan argues from that section). Field-mapping lineage for normalization is inherited from the existing `docs/superpowers/specs/2026-08-12-portkey-export-sync-design.md`; only the three `import_*` fields are new.

---

## Approved Contract

**MeterGraph `/v1/import-sync` server (deployed in metergraph-internal), auth = existing `METERGRAPH_APP_TOKEN` Bearer:**

- `POST /v1/import-sync/acquire` — body `{source:"portkey", source_scope:<one Portkey workspace>, initial_since?:<aware ISO timestamp>, max_window_seconds?:<=3600}`. Responses:
  - `201` acquired → `{lease_id, checkpoint_version, window_start, window_end, lease_expires_at}`
  - `200` caught_up → no window
  - `409` busy → `{retry_at}`
- `POST /v1/import-sync/leases/{lease_id}/renew`
- `POST /v1/import-sync/leases/{lease_id}/complete`
- `DELETE /v1/import-sync/leases/{lease_id}` (release/abandon)
- `GET /v1/import-sync/state?source=portkey&source_scope=...`
- Imported rows must carry `import_source=portkey`, `import_source_scope`, `import_event_id`; the server deduplicates on these.

**Relay behavior (approved):**

- Customer-managed cron MVP only. No UI, no AWS scheduler, no secrets manager.
- Preserve the existing manual `sync portkey export.jsonl` workflow exactly.
- Add Portkey API mode with **no positional export file**, reusing Portkey/MeterGraph config already established where possible.
- One Portkey workspace per MeterGraph app for MVP. `source_scope` is the stable Portkey workspace identifier — **never a secret**.
- No local checkpoint files. acquire/resume/complete happen exclusively through the MeterGraph server.
- `initial_since` is required only for the first server state, but cron may send it every run; the server ignores it once state exists.
- Fixed **1-hour** logical windows; **5-minute overlap owned by the server**; **15-minute renewable lease**.
- Renew during every long phase: Portkey export submission/polling, download, and normalization/upload. A handled failure releases the lease and exits nonzero; a process crash relies on server lease expiry.
- `busy` (active lease) and `caught_up` are clean **no-op exits (exit 0)**.
- `complete` only after **all** required MeterGraph uploads have succeeded.
- Portkey async export flow: submit → poll all jobs → download → normalize to MeterGraph JSONL → upload via the existing push path.
- Volume MVP: a Portkey export result of **>50,000 records** triggers **exactly one** split into **10 time intervals with 1-second overlaps** (no recursion); poll all ten together; complete the original hourly lease only after all ten succeed. Source-event idempotency absorbs boundary duplicates.
- Avoid one-way-door abstractions: isolate window planning / export orchestration from the CLI, but implement only Portkey now.
- Actionable errors, safe secret handling, timeout/backoff consistent with the current project.
- Update README and CLI `--help`: defaults, env vars, cron example, `initial_since` semantics, one-workspace assumption, busy/caught_up behavior, failure/resume behavior.
- Tests written and observed RED before production changes.

---

## Global Constraints

Every task's requirements implicitly include this section.

- **Preserve manual mode byte-for-byte.** `sync portkey EXPORT_FILE [--output PATH]` keeps its current behavior and all existing `tests/providers/test_portkey.py` tests must stay green. New normalization parameters are keyword-only and default to `None` (no behavior change when omitted).
- **Stdlib only.** HTTP is `urllib.request` with an explicit `timeout`, mirroring `providers/langfuse.py` and `push.py`. No new dependency, no `requests`/`httpx`.
- **The project performs no HTTP retry/backoff today** (single attempt, `timeout=10`). Do **not** add speculative retry logic — stay consistent. The only loop is Portkey export **polling**, which uses a fixed interval via an **injected `sleep` callable** (default `time.sleep`) so tests are deterministic and fast. Elapsed time is tracked by summing the poll interval (no `time.time()`), bounded by a safety cap.
- **No local checkpoint files.** All resume state lives on the MeterGraph server. The relay stages downloaded/normalized data only in a `tempfile.TemporaryDirectory` that is removed at the end of the run.
- **`source_scope` is the stable Portkey workspace id and is never a secret** — it may appear in `--help`, logs, and error text. `PORTKEY_API_KEY` and `METERGRAPH_APP_TOKEN` are secrets and must never be printed.
- **Idempotency fields.** API-mode rows add exactly `import_source="portkey"`, `import_source_scope=<source_scope>`, `import_event_id=<Portkey row id>`. `import_event_id` reuses the Portkey request `id` (same value already mapped to `request_id`/`span_id`) so re-runs and overlap regions dedupe server-side.
- **Exit codes.** `completed`, `caught_up`, `busy` → exit 0 (all clean). Any handled failure → exit 1 after releasing the lease (`DELETE`), except lease-lost during renew/complete (lease already gone → exit 1 without an abandon call). Process crash → no cleanup, server lease expiry handles it.
- **`complete` fires only after every push in the run reported zero failed rows.**
- **Volume split is one-shot, never recursive:** threshold `> 50_000` on a completed full-window export triggers exactly one split into 10 sub-windows with 1-second boundary overlaps; those ten become the download set (the oversized full-window export is discarded, not downloaded).
- **Base URL reuse:** the MeterGraph import-sync client and `push_file` target the same server; base URL resolves from `METERGRAPH_INGEST_URL` (falling back to `push.DEFAULT_INGEST_URL`), a single source of truth. Portkey base URL resolves from `PORTKEY_BASE_URL` (falling back to `portkey_export.DEFAULT_PORTKEY_URL`).
- **Test env isolation:** any new env var the CLI reads must be added to `tests/conftest.py`'s `ENV_VARS_READ_BY_CLI` set (credentials via `CREDENTIAL_SPECS` are auto-included; non-credential vars `PORTKEY_WORKSPACE`/`PORTKEY_BASE_URL` are added explicitly, exactly as `LANGFUSE_BASE_URL`/`METERGRAPH_INGEST_URL` already are).
- **Synthetic test data only.** No customer content in tests, fixtures, warnings, or committed files.

---

### Task 1: Pure window planning (`window.py`)

**Files:**
- Create: `src/metergraphrelay/window.py`
- Test: `tests/test_window.py`

**Interfaces:**
- Consumes: nothing (leaf module, stdlib `datetime`/`dataclasses` only).
- Produces (used by Task 5):
```python
@dataclass(frozen=True)
class TimeWindow:
    start: str   # aware ISO 8601
    end: str     # aware ISO 8601

SPLIT_PARTS: int = 10
SPLIT_OVERLAP_SECONDS: int = 1

def split_window(
    window: TimeWindow, *, parts: int = SPLIT_PARTS, overlap_seconds: int = SPLIT_OVERLAP_SECONDS
) -> list[TimeWindow]: ...
```

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_window.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_window.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'metergraphrelay.window'`.

- [ ] **Step 3: Implement `window.py`**

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

SPLIT_PARTS = 10
SPLIT_OVERLAP_SECONDS = 1


@dataclass(frozen=True)
class TimeWindow:
    start: str  # aware ISO 8601
    end: str    # aware ISO 8601


def _parse_aware(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_window.py -v`
Expected: PASS — all six tests.

- [ ] **Step 5: Commit**

```bash
git add src/metergraphrelay/window.py tests/test_window.py
git commit -m "feat(sync): add pure time-window planning for the volume split"
```

---

### Task 2: `ImportContext` normalization fields on the Portkey normalizer

**Files:**
- Modify: `src/metergraphrelay/providers/portkey.py`
- Test: `tests/providers/test_portkey.py` (append; do not disturb existing tests)

**Interfaces:**
- Consumes: existing `normalize_portkey_row`, `convert_portkey_export`.
- Produces (used by Task 5):
```python
@dataclass(frozen=True)
class ImportContext:
    source: str        # "portkey"
    source_scope: str

def normalize_portkey_row(row: dict, *, import_context: ImportContext | None = None) -> dict: ...
def convert_portkey_export(
    input_path: str, output_path: str, *, import_context: ImportContext | None = None
) -> tuple[int, int]: ...
```
When `import_context is None` the output dict is identical to today (existing tests prove this). When provided, three keys are added: `import_source`, `import_source_scope`, `import_event_id` (`= row["id"]`).

- [ ] **Step 1: Write the failing tests** (append to `tests/providers/test_portkey.py`; reuses the existing `_responses_row` helper)

```python
from metergraphrelay.providers.portkey import ImportContext  # add to existing imports


def test_normalize_portkey_row_without_import_context_omits_import_fields():
    result = normalize_portkey_row(_responses_row())
    assert "import_source" not in result
    assert "import_source_scope" not in result
    assert "import_event_id" not in result


def test_normalize_portkey_row_with_import_context_adds_dedup_fields():
    ctx = ImportContext(source="portkey", source_scope="ws-acme")
    result = normalize_portkey_row(_responses_row(id="pk-req-1"), import_context=ctx)
    assert result["import_source"] == "portkey"
    assert result["import_source_scope"] == "ws-acme"
    assert result["import_event_id"] == "pk-req-1"
    # import_event_id is the stable Portkey request id, same value as request_id.
    assert result["import_event_id"] == result["request_id"]


def test_convert_portkey_export_threads_import_context_into_every_row(tmp_path):
    ctx = ImportContext(source="portkey", source_scope="ws-acme")
    input_path = tmp_path / "raw.jsonl"
    input_path.write_text(
        json.dumps(_responses_row(id="row-1", trace_id="t-1")) + "\n"
        + json.dumps(_chat_completion_row(id="row-2", trace_id="t-2")) + "\n"
    )
    output_path = tmp_path / "converted.jsonl"

    converted, skipped = convert_portkey_export(
        str(input_path), str(output_path), import_context=ctx
    )

    assert (converted, skipped) == (2, 0)
    rows = [json.loads(line) for line in output_path.read_text().splitlines()]
    assert [r["import_event_id"] for r in rows] == ["row-1", "row-2"]
    assert all(r["import_source"] == "portkey" for r in rows)
    assert all(r["import_source_scope"] == "ws-acme" for r in rows)


def test_convert_portkey_export_without_context_keeps_rows_free_of_import_fields(tmp_path):
    input_path = tmp_path / "raw.jsonl"
    input_path.write_text(json.dumps(_responses_row(id="row-1", trace_id="t-1")) + "\n")
    output_path = tmp_path / "converted.jsonl"

    convert_portkey_export(str(input_path), str(output_path))

    row = json.loads(output_path.read_text().splitlines()[0])
    assert "import_source" not in row
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/providers/test_portkey.py -v -k import_context`
Expected: FAIL — `ImportError: cannot import name 'ImportContext'`.

- [ ] **Step 3: Implement the extension in `providers/portkey.py`**

- Add near the top, after the imports:
```python
from dataclasses import dataclass


@dataclass(frozen=True)
class ImportContext:
    source: str
    source_scope: str
```
- Change the signature to `def normalize_portkey_row(row: dict, *, import_context: ImportContext | None = None) -> dict:`.
- Just before the `return {...}`, build the base result dict, then conditionally add the fields. Concretely, keep the existing returned dict but assign it to a variable and append:
```python
    result = {
        ...  # unchanged existing mapping
    }
    if import_context is not None:
        result["import_source"] = import_context.source
        result["import_source_scope"] = import_context.source_scope
        result["import_event_id"] = request_id  # request_id is already row["id"]
    return result
```
- Change `convert_portkey_export` signature to accept `*, import_context: ImportContext | None = None` and pass it through: `normalized = normalize_portkey_row(row, import_context=import_context)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/providers/test_portkey.py -v`
Expected: PASS — the four new tests **and** every pre-existing test in the file (proving manual mode is unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/metergraphrelay/providers/portkey.py tests/providers/test_portkey.py
git commit -m "feat(portkey): thread optional ImportContext dedup fields through normalization"
```

---

### Task 3: MeterGraph import-sync client (`metergraph_sync.py`)

**Files:**
- Create: `src/metergraphrelay/metergraph_sync.py`
- Test: `tests/test_metergraph_sync.py`

**Interfaces:**
- Consumes: `push.DEFAULT_INGEST_URL` (base URL default only).
- Produces (used by Tasks 5 & 6):
```python
class MeterGraphSyncError(Exception): ...
class LeaseLostError(MeterGraphSyncError): ...   # 404/409/410 on renew/complete/abandon

@dataclass(frozen=True)
class AcquiredLease:
    lease_id: str
    checkpoint_version: object     # opaque; echoed only, server owns it
    window_start: str
    window_end: str
    lease_expires_at: str

@dataclass(frozen=True)
class AcquireResult:
    status: str                    # "acquired" | "caught_up" | "busy"
    lease: AcquiredLease | None = None
    retry_at: str | None = None    # set only when status == "busy"

class MeterGraphSyncClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 10.0): ...
    def acquire(self, *, source: str, source_scope: str,
                initial_since: str | None = None,
                max_window_seconds: int | None = None) -> AcquireResult: ...
    def renew(self, lease_id: str) -> str: ...        # returns new lease_expires_at
    def complete(self, lease_id: str) -> None: ...
    def abandon(self, lease_id: str) -> None: ...      # DELETE
    def get_state(self, *, source: str, source_scope: str) -> dict: ...
```

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_metergraph_sync.py
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from metergraphrelay.metergraph_sync import (
    AcquireResult,
    LeaseLostError,
    MeterGraphSyncClient,
    MeterGraphSyncError,
)

BASE = "https://mg.example.com"


def _resp(status, body: bytes = b""):
    r = MagicMock()
    r.status = status
    r.read.return_value = body
    r.__enter__.return_value = r
    r.__exit__.return_value = False
    return r


def _http_error(code, body: bytes = b"{}"):
    return urllib.error.HTTPError(
        url=f"{BASE}/x", code=code, msg="err", hdrs=None, fp=MagicMock(read=lambda: body)
    )


def _client():
    return MeterGraphSyncClient(BASE, "tok-123")


def test_acquire_201_returns_acquired_lease_and_sends_bearer_and_body():
    body = json.dumps(
        {
            "lease_id": "lease-1",
            "checkpoint_version": 7,
            "window_start": "2026-08-19T00:00:00+00:00",
            "window_end": "2026-08-19T01:00:00+00:00",
            "lease_expires_at": "2026-08-19T00:15:00+00:00",
        }
    ).encode()
    with patch("metergraphrelay.metergraph_sync.urllib.request.urlopen") as mock:
        mock.return_value = _resp(201, body)
        result = _client().acquire(
            source="portkey",
            source_scope="ws-acme",
            initial_since="2026-08-01T00:00:00+00:00",
            max_window_seconds=3600,
        )

    request = mock.call_args.args[0]
    assert request.full_url == f"{BASE}/v1/import-sync/acquire"
    assert request.get_header("Authorization") == "Bearer tok-123"
    assert request.method == "POST"
    sent = json.loads(request.data)
    assert sent == {
        "source": "portkey",
        "source_scope": "ws-acme",
        "initial_since": "2026-08-01T00:00:00+00:00",
        "max_window_seconds": 3600,
    }
    assert result.status == "acquired"
    assert result.lease.lease_id == "lease-1"
    assert result.lease.window_start == "2026-08-19T00:00:00+00:00"
    assert result.lease.window_end == "2026-08-19T01:00:00+00:00"
    assert result.lease.lease_expires_at == "2026-08-19T00:15:00+00:00"


def test_acquire_omits_optional_fields_when_not_given():
    with patch("metergraphrelay.metergraph_sync.urllib.request.urlopen") as mock:
        mock.return_value = _resp(200, b"{}")
        _client().acquire(source="portkey", source_scope="ws-acme")
    sent = json.loads(mock.call_args.args[0].data)
    assert sent == {"source": "portkey", "source_scope": "ws-acme"}


def test_acquire_200_returns_caught_up():
    with patch("metergraphrelay.metergraph_sync.urllib.request.urlopen") as mock:
        mock.return_value = _resp(200, b"{}")
        result = _client().acquire(source="portkey", source_scope="ws-acme")
    assert result == AcquireResult(status="caught_up")


def test_acquire_409_returns_busy_with_retry_at():
    with patch("metergraphrelay.metergraph_sync.urllib.request.urlopen") as mock:
        mock.side_effect = _http_error(409, json.dumps({"retry_at": "2026-08-19T00:20:00+00:00"}).encode())
        result = _client().acquire(source="portkey", source_scope="ws-acme")
    assert result.status == "busy"
    assert result.retry_at == "2026-08-19T00:20:00+00:00"


def test_acquire_unexpected_status_raises_sync_error():
    with patch("metergraphrelay.metergraph_sync.urllib.request.urlopen") as mock:
        mock.side_effect = _http_error(500, b"{}")
        with pytest.raises(MeterGraphSyncError, match="500"):
            _client().acquire(source="portkey", source_scope="ws-acme")


def test_renew_posts_to_lease_path_and_returns_new_expiry():
    body = json.dumps({"lease_expires_at": "2026-08-19T00:30:00+00:00"}).encode()
    with patch("metergraphrelay.metergraph_sync.urllib.request.urlopen") as mock:
        mock.return_value = _resp(200, body)
        expires = _client().renew("lease-1")
    request = mock.call_args.args[0]
    assert request.full_url == f"{BASE}/v1/import-sync/leases/lease-1/renew"
    assert request.method == "POST"
    assert expires == "2026-08-19T00:30:00+00:00"


def test_renew_404_raises_lease_lost():
    with patch("metergraphrelay.metergraph_sync.urllib.request.urlopen") as mock:
        mock.side_effect = _http_error(404, b"{}")
        with pytest.raises(LeaseLostError):
            _client().renew("lease-1")


def test_complete_posts_to_complete_path():
    with patch("metergraphrelay.metergraph_sync.urllib.request.urlopen") as mock:
        mock.return_value = _resp(200, b"{}")
        _client().complete("lease-1")
    request = mock.call_args.args[0]
    assert request.full_url == f"{BASE}/v1/import-sync/leases/lease-1/complete"
    assert request.method == "POST"


def test_abandon_issues_delete_to_lease_path():
    with patch("metergraphrelay.metergraph_sync.urllib.request.urlopen") as mock:
        mock.return_value = _resp(200, b"{}")
        _client().abandon("lease-1")
    request = mock.call_args.args[0]
    assert request.full_url == f"{BASE}/v1/import-sync/leases/lease-1"
    assert request.method == "DELETE"


def test_abandon_swallows_lease_lost_as_noop():
    # Abandoning an already-expired/absent lease is not an error worth failing on.
    with patch("metergraphrelay.metergraph_sync.urllib.request.urlopen") as mock:
        mock.side_effect = _http_error(404, b"{}")
        _client().abandon("lease-1")  # must not raise


def test_get_state_builds_query_and_returns_payload():
    body = json.dumps({"source": "portkey", "checkpoint_version": 7}).encode()
    with patch("metergraphrelay.metergraph_sync.urllib.request.urlopen") as mock:
        mock.return_value = _resp(200, body)
        state = _client().get_state(source="portkey", source_scope="ws-acme")
    request = mock.call_args.args[0]
    assert request.full_url == f"{BASE}/v1/import-sync/state?source=portkey&source_scope=ws-acme"
    assert request.method == "GET"
    assert state["checkpoint_version"] == 7


def test_network_error_raises_sync_error():
    with patch("metergraphrelay.metergraph_sync.urllib.request.urlopen") as mock:
        mock.side_effect = urllib.error.URLError("connection refused")
        with pytest.raises(MeterGraphSyncError, match="connection refused"):
            _client().acquire(source="portkey", source_scope="ws-acme")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_metergraph_sync.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'metergraphrelay.metergraph_sync'`.

- [ ] **Step 3: Implement `metergraph_sync.py`**

```python
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .push import DEFAULT_INGEST_URL  # single source of truth for the server host

ACQUIRE_PATH = "/v1/import-sync/acquire"
LEASES_PATH = "/v1/import-sync/leases"
STATE_PATH = "/v1/import-sync/state"
_LEASE_LOST_STATUSES = frozenset({404, 409, 410})


class MeterGraphSyncError(Exception):
    """Raised when the MeterGraph import-sync API errors or returns an unusable body."""


class LeaseLostError(MeterGraphSyncError):
    """Raised when a lease is no longer held (renew/complete on an expired/stolen lease)."""


@dataclass(frozen=True)
class AcquiredLease:
    lease_id: str
    checkpoint_version: object
    window_start: str
    window_end: str
    lease_expires_at: str


@dataclass(frozen=True)
class AcquireResult:
    status: str
    lease: AcquiredLease | None = None
    retry_at: str | None = None


class MeterGraphSyncClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 10.0) -> None:
        self._base = (base_url or DEFAULT_INGEST_URL).rstrip("/")
        self._token = token
        self._timeout = timeout

    def _request(self, method: str, path: str, *, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self._token}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self._base}{path}", data=data, headers=headers, method=method
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            raw = response.read()
            return response.status, self._parse(raw)

    @staticmethod
    def _parse(raw: bytes) -> dict:
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MeterGraphSyncError(f"MeterGraph import-sync returned invalid JSON: {exc}") from exc
        return payload if isinstance(payload, dict) else {"_": payload}

    def acquire(self, *, source, source_scope, initial_since=None, max_window_seconds=None) -> AcquireResult:
        body = {"source": source, "source_scope": source_scope}
        if initial_since is not None:
            body["initial_since"] = initial_since
        if max_window_seconds is not None:
            body["max_window_seconds"] = max_window_seconds
        try:
            status, payload = self._request("POST", ACQUIRE_PATH, body=body)
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                busy = self._parse(exc.read())
                return AcquireResult(status="busy", retry_at=busy.get("retry_at"))
            raise MeterGraphSyncError(f"acquire failed: HTTP {exc.code} {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise MeterGraphSyncError(f"acquire failed: {exc.reason}") from exc
        if status == 201:
            return AcquireResult(
                status="acquired",
                lease=AcquiredLease(
                    lease_id=payload["lease_id"],
                    checkpoint_version=payload.get("checkpoint_version"),
                    window_start=payload["window_start"],
                    window_end=payload["window_end"],
                    lease_expires_at=payload["lease_expires_at"],
                ),
            )
        return AcquireResult(status="caught_up")

    def renew(self, lease_id: str) -> str:
        payload = self._lease_call("POST", f"{LEASES_PATH}/{lease_id}/renew")
        return payload.get("lease_expires_at", "")

    def complete(self, lease_id: str) -> None:
        self._lease_call("POST", f"{LEASES_PATH}/{lease_id}/complete")

    def abandon(self, lease_id: str) -> None:
        try:
            self._lease_call("DELETE", f"{LEASES_PATH}/{lease_id}")
        except LeaseLostError:
            return  # already gone — nothing to release

    def get_state(self, *, source: str, source_scope: str) -> dict:
        query = urllib.parse.urlencode({"source": source, "source_scope": source_scope})
        try:
            _, payload = self._request("GET", f"{STATE_PATH}?{query}")
        except urllib.error.HTTPError as exc:
            raise MeterGraphSyncError(f"get_state failed: HTTP {exc.code} {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise MeterGraphSyncError(f"get_state failed: {exc.reason}") from exc
        return payload

    def _lease_call(self, method: str, path: str) -> dict:
        try:
            _, payload = self._request(method, path)
        except urllib.error.HTTPError as exc:
            if exc.code in _LEASE_LOST_STATUSES:
                raise LeaseLostError(f"lease no longer held: HTTP {exc.code} {exc.reason}") from exc
            raise MeterGraphSyncError(f"{method} {path} failed: HTTP {exc.code} {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise MeterGraphSyncError(f"{method} {path} failed: {exc.reason}") from exc
        return payload
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_metergraph_sync.py -v`
Expected: PASS — all tests.

- [ ] **Step 5: Commit**

```bash
git add src/metergraphrelay/metergraph_sync.py tests/test_metergraph_sync.py
git commit -m "feat(sync): add MeterGraph import-sync client (acquire/renew/complete/abandon/state)"
```

---

### Task 4: Portkey async export client (`providers/portkey_export.py`)

> **ASSUMPTION — VERIFY BEFORE IMPLEMENTING WIRE FORMAT.** Portkey's Logs Export HTTP endpoints, request bodies, and response field names are **not verifiable from inside this repo**. This module is the single quarantine boundary for that risk: the *interface* (`PortkeyExportClient` + `PortkeyExportJob`) is what the orchestrator and its tests depend on, and it is stable. The concrete request paths, request-body keys, and response-field names live in the named constants at the top of the file (`SUBMIT_PATH`, `JOB_PATH_TEMPLATE`, `_STATUS_FIELD`, `_STATUS_MAP`, `_RECORD_COUNT_FIELD`, `_DOWNLOAD_FIELD`). Before writing production HTTP, confirm these against Portkey's Logs Export API docs and adjust **only** these constants and the localized tests in this task — no other module changes. The tests below pin the *shapes the code expects*; if the real API differs, update the fixtures and constants together.

**Files:**
- Create: `src/metergraphrelay/providers/portkey_export.py`
- Test: `tests/providers/test_portkey_export.py`

**Interfaces:**
- Consumes: nothing outside stdlib.
- Produces (used by Task 5):
```python
class PortkeyExportError(Exception): ...

STATUS_COMPLETED: str = "completed"
STATUS_FAILED: str = "failed"

@dataclass(frozen=True)
class PortkeyExportJob:
    job_id: str
    status: str                 # normalized: "pending" | "running" | "completed" | "failed"
    record_count: int | None    # populated when completed
    download_token: str | None  # opaque handle passed back to download_to

    @property
    def is_terminal(self) -> bool: ...        # status in {"completed", "failed"}
    @property
    def is_success(self) -> bool: ...         # status == "completed"

class PortkeyExportClient:
    def __init__(self, api_key: str, *, workspace: str,
                 base_url: str = DEFAULT_PORTKEY_URL, timeout: float = 30.0): ...
    def submit_export(self, *, window_start: str, window_end: str) -> PortkeyExportJob: ...
    def get_job(self, job_id: str) -> PortkeyExportJob: ...
    def download_to(self, job: PortkeyExportJob, dest_path: str) -> int: ...  # returns rows written
```
`workspace` is fixed at construction (one workspace per app, MVP) and sent as `source_scope`/workspace on submit; `submit_export` takes only the window. Auth header assumption: `x-portkey-api-key: <api_key>` (a named constant — verify).

- [ ] **Step 1: Write the failing tests**

```python
# tests/providers/test_portkey_export.py
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from metergraphrelay.providers.portkey_export import (
    STATUS_COMPLETED,
    PortkeyExportClient,
    PortkeyExportError,
    PortkeyExportJob,
)

BASE = "https://api.portkey.example"


def _resp(status, body: bytes):
    r = MagicMock()
    r.status = status
    r.read.return_value = body
    r.__enter__.return_value = r
    r.__exit__.return_value = False
    return r


def _client():
    return PortkeyExportClient("pk-secret", workspace="ws-acme", base_url=BASE)


def test_submit_export_sends_workspace_window_and_api_key_header():
    body = json.dumps({"id": "job-1", "status": "queued"}).encode()
    with patch("metergraphrelay.providers.portkey_export.urllib.request.urlopen") as mock:
        mock.return_value = _resp(200, body)
        job = _client().submit_export(
            window_start="2026-08-19T00:00:00+00:00", window_end="2026-08-19T01:00:00+00:00"
        )
    request = mock.call_args.args[0]
    assert request.method == "POST"
    assert request.get_header("X-portkey-api-key") == "pk-secret"  # header name is case-normalized by urllib
    sent = json.loads(request.data)
    assert sent["workspace_id"] == "ws-acme"
    assert sent["start_time"] == "2026-08-19T00:00:00+00:00"
    assert sent["end_time"] == "2026-08-19T01:00:00+00:00"
    assert job.job_id == "job-1"
    assert not job.is_terminal


def test_get_job_normalizes_completed_status_and_record_count():
    body = json.dumps(
        {"id": "job-1", "status": "success", "total_records": 42, "download_url": "https://dl/job-1"}
    ).encode()
    with patch("metergraphrelay.providers.portkey_export.urllib.request.urlopen") as mock:
        mock.return_value = _resp(200, body)
        job = _client().get_job("job-1")
    assert job.status == STATUS_COMPLETED
    assert job.is_terminal and job.is_success
    assert job.record_count == 42
    assert job.download_token == "https://dl/job-1"


def test_get_job_normalizes_failed_status():
    body = json.dumps({"id": "job-1", "status": "failed"}).encode()
    with patch("metergraphrelay.providers.portkey_export.urllib.request.urlopen") as mock:
        mock.return_value = _resp(200, body)
        job = _client().get_job("job-1")
    assert job.is_terminal
    assert not job.is_success


def test_download_to_writes_jsonl_and_returns_row_count(tmp_path):
    job = PortkeyExportJob(
        job_id="job-1", status=STATUS_COMPLETED, record_count=2, download_token="https://dl/job-1"
    )
    payload = b'{"id":"r1"}\n{"id":"r2"}\n'
    dest = tmp_path / "raw.jsonl"
    with patch("metergraphrelay.providers.portkey_export.urllib.request.urlopen") as mock:
        mock.return_value = _resp(200, payload)
        written = _client().download_to(job, str(dest))
    assert written == 2
    assert dest.read_bytes() == payload


def test_download_to_raises_when_no_download_token():
    job = PortkeyExportJob(job_id="job-1", status=STATUS_COMPLETED, record_count=0, download_token=None)
    with pytest.raises(PortkeyExportError, match="no download"):
        _client().download_to(job, "/tmp/whatever.jsonl")


def test_http_error_raises_portkey_export_error():
    with patch("metergraphrelay.providers.portkey_export.urllib.request.urlopen") as mock:
        mock.side_effect = urllib.error.HTTPError(
            url=f"{BASE}/x", code=401, msg="Unauthorized", hdrs=None, fp=None
        )
        with pytest.raises(PortkeyExportError, match="401"):
            _client().get_job("job-1")


def test_network_error_raises_portkey_export_error():
    with patch("metergraphrelay.providers.portkey_export.urllib.request.urlopen") as mock:
        mock.side_effect = urllib.error.URLError("connection refused")
        with pytest.raises(PortkeyExportError, match="connection refused"):
            _client().get_job("job-1")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/providers/test_portkey_export.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'metergraphrelay.providers.portkey_export'`.

- [ ] **Step 3: Implement `providers/portkey_export.py`**

```python
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

# --- ASSUMED Portkey Logs Export wire contract — verify against Portkey docs ---
DEFAULT_PORTKEY_URL = "https://api.portkey.ai"
SUBMIT_PATH = "/v1/logs/exports"
JOB_PATH_TEMPLATE = "/v1/logs/exports/{job_id}"
_API_KEY_HEADER = "x-portkey-api-key"
_STATUS_FIELD = "status"
_RECORD_COUNT_FIELD = "total_records"
_DOWNLOAD_FIELD = "download_url"
_JOB_ID_FIELD = "id"
# Map Portkey's status vocabulary onto our normalized four-state model.
_STATUS_MAP = {
    "queued": "pending",
    "pending": "pending",
    "running": "running",
    "in_progress": "running",
    "success": "completed",
    "completed": "completed",
    "failed": "failed",
    "error": "failed",
}
# --- end ASSUMED contract ---

STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
_TERMINAL = frozenset({STATUS_COMPLETED, STATUS_FAILED})


class PortkeyExportError(Exception):
    """Raised when the Portkey Logs Export API errors or returns an unusable body."""


@dataclass(frozen=True)
class PortkeyExportJob:
    job_id: str
    status: str
    record_count: int | None
    download_token: str | None

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL

    @property
    def is_success(self) -> bool:
        return self.status == STATUS_COMPLETED


class PortkeyExportClient:
    def __init__(self, api_key: str, *, workspace: str, base_url: str = DEFAULT_PORTKEY_URL, timeout: float = 30.0):
        self._api_key = api_key
        self._workspace = workspace
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    def _request(self, method: str, path: str, *, body: dict | None = None) -> bytes:
        data = json.dumps(body).encode() if body is not None else None
        headers = {_API_KEY_HEADER: self._api_key}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"{self._base}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise PortkeyExportError(f"Portkey export request failed: HTTP {exc.code} {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise PortkeyExportError(f"Portkey export request failed: {exc.reason}") from exc

    def _job_from_payload(self, raw: bytes) -> PortkeyExportJob:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PortkeyExportError(f"Portkey export returned invalid JSON: {exc}") from exc
        if not isinstance(payload, dict) or _JOB_ID_FIELD not in payload:
            raise PortkeyExportError("Portkey export response missing job id")
        raw_status = str(payload.get(_STATUS_FIELD, "")).lower()
        status = _STATUS_MAP.get(raw_status, "running")
        count = payload.get(_RECORD_COUNT_FIELD)
        return PortkeyExportJob(
            job_id=str(payload[_JOB_ID_FIELD]),
            status=status,
            record_count=count if isinstance(count, int) else None,
            download_token=payload.get(_DOWNLOAD_FIELD),
        )

    def submit_export(self, *, window_start: str, window_end: str) -> PortkeyExportJob:
        body = {"workspace_id": self._workspace, "start_time": window_start, "end_time": window_end}
        return self._job_from_payload(self._request("POST", SUBMIT_PATH, body=body))

    def get_job(self, job_id: str) -> PortkeyExportJob:
        return self._job_from_payload(self._request("GET", JOB_PATH_TEMPLATE.format(job_id=job_id)))

    def download_to(self, job: PortkeyExportJob, dest_path: str) -> int:
        if not job.download_token:
            raise PortkeyExportError(f"job {job.job_id} has no download token")
        request = urllib.request.Request(job.download_token, headers={_API_KEY_HEADER: self._api_key}, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response, open(dest_path, "wb") as dst:
                raw = response.read()
                dst.write(raw)
        except urllib.error.HTTPError as exc:
            raise PortkeyExportError(f"Portkey download failed: HTTP {exc.code} {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise PortkeyExportError(f"Portkey download failed: {exc.reason}") from exc
        return sum(1 for line in raw.splitlines() if line.strip())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/providers/test_portkey_export.py -v`
Expected: PASS — all tests.

- [ ] **Step 5: Commit**

```bash
git add src/metergraphrelay/providers/portkey_export.py tests/providers/test_portkey_export.py
git commit -m "feat(portkey): add Portkey async Logs Export client (submit/poll/download)"
```

---

### Task 5: Sync orchestrator (`portkey_sync.py`) + fake end-to-end test

**Files:**
- Create: `src/metergraphrelay/portkey_sync.py`
- Test: `tests/test_portkey_sync.py`

**Interfaces:**
- Consumes: `MeterGraphSyncClient`/`AcquireResult`/`LeaseLostError`/`MeterGraphSyncError` (Task 3); `PortkeyExportClient`/`PortkeyExportJob`/`PortkeyExportError` (Task 4); `TimeWindow`/`split_window` (Task 1); `ImportContext`/`convert_portkey_export` (Task 2); `push.push_file`.
- Produces (used by Task 6):
```python
PORTKEY_SOURCE: str = "portkey"
VOLUME_SPLIT_THRESHOLD: int = 50_000
POLL_INTERVAL_SECONDS: float = 15.0
MAX_POLL_SECONDS: float = 3300.0   # 55 min safety cap; renew keeps the lease alive within it

@dataclass(frozen=True)
class SyncOutcome:
    status: str          # "completed" | "caught_up" | "busy" | "failed"
    detail: str          # human-facing summary line
    exit_code: int       # 0 for completed/caught_up/busy; 1 for failed
    pushed: int = 0
    failed: int = 0
    skipped: int = 0

def run_portkey_sync(
    *,
    mg_client, pk_client,
    source_scope: str,
    initial_since: str | None,
    max_window_seconds: int,
    push_token: str,
    ingest_base_url: str | None,
    work_dir: str | None = None,
    sleep=time.sleep,
    poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
    max_poll_seconds: float = MAX_POLL_SECONDS,
    push=push_file,
) -> SyncOutcome: ...
```

- [ ] **Step 1: Write the failing tests** (fakes + orchestration + the end-to-end test)

```python
# tests/test_portkey_sync.py
import json

from metergraphrelay.metergraph_sync import AcquiredLease, AcquireResult, LeaseLostError
from metergraphrelay.providers.portkey_export import STATUS_COMPLETED, PortkeyExportJob
from metergraphrelay.portkey_sync import VOLUME_SPLIT_THRESHOLD, run_portkey_sync

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

    def acquire(self, **kwargs):
        self.acquire_kwargs = kwargs
        return self._acquire

    def renew(self, lease_id):
        if self.renew_error:
            raise self.renew_error
        self.renewed += 1
        return "2026-08-19T00:30:00+00:00"

    def complete(self, lease_id):
        self.completed.append(lease_id)

    def abandon(self, lease_id):
        self.abandoned.append(lease_id)


class FakePortkey:
    """Jobs complete immediately; rows keyed by (window_start, window_end)."""
    def __init__(self, rows_by_window, record_counts=None):
        self._rows = rows_by_window
        self._counts = record_counts or {}
        self.submitted = []

    def submit_export(self, *, window_start, window_end):
        key = (window_start, window_end)
        self.submitted.append(key)
        jid = f"job-{len(self.submitted)}"
        count = self._counts.get(key, len(self._rows.get(key, [])))
        self._pending = getattr(self, "_pending", {})
        self._pending[jid] = key
        return PortkeyExportJob(jid, STATUS_COMPLETED, count, f"dl://{jid}")

    def get_job(self, job_id):
        key = self._pending[job_id]
        count = self._counts.get(key, len(self._rows.get(key, [])))
        return PortkeyExportJob(job_id, STATUS_COMPLETED, count, f"dl://{job_id}")

    def download_to(self, job, dest_path):
        key = self._pending[job.job_id]
        rows = self._rows.get(key, [])
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


def test_caught_up_is_a_clean_noop_exit():
    mg = FakeMeterGraph(AcquireResult(status="caught_up"))
    pk = FakePortkey({})
    pushes = []
    outcome = _run(mg, pk, pushes)
    assert outcome.status == "caught_up"
    assert outcome.exit_code == 0
    assert pk.submitted == []
    assert mg.completed == []


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


def test_end_to_end_happy_path_acquires_downloads_normalizes_pushes_and_completes():
    rows = [_portkey_row("r1"), _portkey_row("r2")]
    pk = FakePortkey({(WINDOW_START, WINDOW_END): rows})
    mg = FakeMeterGraph(_acquired())
    pushes = []

    outcome = _run(mg, pk, pushes)

    assert outcome.status == "completed"
    assert outcome.exit_code == 0
    assert pk.submitted == [(WINDOW_START, WINDOW_END)]
    assert mg.completed == ["lease-1"]        # complete only after a successful push
    assert mg.abandoned == []
    assert mg.renewed >= 1                      # renewed during long phases
    # Pushed rows carry the server-dedup fields:
    pushed_rows = [r for p in pushes for r in p["rows"]]
    assert {r["import_event_id"] for r in pushed_rows} == {"r1", "r2"}
    assert all(r["import_source"] == "portkey" for r in pushed_rows)
    assert all(r["import_source_scope"] == "ws-acme" for r in pushed_rows)
    assert pushes[0]["token"] == "tok-123"


def test_over_threshold_triggers_one_split_into_ten_overlapping_windows():
    full = (WINDOW_START, WINDOW_END)
    # Full-window export reports > 50k -> discard, split into 10 sub-windows.
    counts = {full: VOLUME_SPLIT_THRESHOLD + 1}
    # Give every sub-window one row so downloads/pushes happen.
    pk = FakePortkey({full: []}, record_counts=counts)

    # Sub-window rows are supplied lazily: FakePortkey.download_to reads by key,
    # so seed rows for whatever sub-windows the orchestrator submits.
    original_submit = pk.submit_export
    def seeding_submit(*, window_start, window_end):
        pk._rows.setdefault((window_start, window_end), [_portkey_row(f"r-{window_start}")])
        return original_submit(window_start=window_start, window_end=window_end)
    pk.submit_export = seeding_submit

    mg = FakeMeterGraph(_acquired())
    pushes = []
    outcome = _run(mg, pk, pushes)

    assert outcome.status == "completed"
    # 1 full-window submit + 10 sub-window submits, exactly one split (no recursion).
    assert len(pk.submitted) == 1 + 10
    sub_windows = pk.submitted[1:]
    assert len(sub_windows) == 10
    assert sub_windows[0][0] == WINDOW_START            # first sub-window starts at window start
    assert sub_windows[-1][1] == WINDOW_END             # last sub-window ends at window end
    assert mg.completed == ["lease-1"]                   # completed only after all ten pushed


def test_push_failure_abandons_lease_and_exits_nonzero_without_completing():
    rows = [_portkey_row("r1")]
    pk = FakePortkey({(WINDOW_START, WINDOW_END): rows})
    mg = FakeMeterGraph(_acquired())

    def failing_push(path, token, base_url=None):
        return (0, 1)  # one row failed

    outcome = _run(mg, pk, [], push=failing_push)

    assert outcome.status == "failed"
    assert outcome.exit_code == 1
    assert mg.completed == []
    assert mg.abandoned == ["lease-1"]        # handled failure releases the lease


def test_portkey_error_abandons_lease_and_exits_nonzero():
    mg = FakeMeterGraph(_acquired())

    class Boom(FakePortkey):
        def submit_export(self, *, window_start, window_end):
            from metergraphrelay.providers.portkey_export import PortkeyExportError
            raise PortkeyExportError("submit boom")

    outcome = _run(mg, Boom({}), [])
    assert outcome.status == "failed"
    assert outcome.exit_code == 1
    assert mg.abandoned == ["lease-1"]


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_portkey_sync.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'metergraphrelay.portkey_sync'`.

- [ ] **Step 3: Implement `portkey_sync.py`**

```python
from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass

from .metergraph_sync import LeaseLostError, MeterGraphSyncError
from .providers.portkey import ImportContext, convert_portkey_export
from .providers.portkey_export import PortkeyExportError, PortkeyExportJob
from .push import push_file
from .window import TimeWindow, split_window

PORTKEY_SOURCE = "portkey"
VOLUME_SPLIT_THRESHOLD = 50_000
POLL_INTERVAL_SECONDS = 15.0
MAX_POLL_SECONDS = 3300.0


@dataclass(frozen=True)
class SyncOutcome:
    status: str
    detail: str
    exit_code: int
    pushed: int = 0
    failed: int = 0
    skipped: int = 0


def run_portkey_sync(
    *,
    mg_client,
    pk_client,
    source_scope,
    initial_since,
    max_window_seconds,
    push_token,
    ingest_base_url,
    work_dir=None,
    sleep=time.sleep,
    poll_interval_seconds=POLL_INTERVAL_SECONDS,
    max_poll_seconds=MAX_POLL_SECONDS,
    push=push_file,
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
        return SyncOutcome("busy", f"Another sync holds the lease; retry at {acquire.retry_at}.", 0)

    lease = acquire.lease
    ctx = ImportContext(source=PORTKEY_SOURCE, source_scope=source_scope)
    try:
        with tempfile.TemporaryDirectory(dir=work_dir) as staging:
            jobs = _collect_export_jobs(
                pk_client, lease, mg_client,
                sleep=sleep, poll_interval=poll_interval_seconds, max_poll_seconds=max_poll_seconds,
            )
            mg_client.renew(lease.lease_id)  # renew before the download/normalize/upload phase
            pushed = failed = skipped = 0
            for i, job in enumerate(jobs):
                raw = os.path.join(staging, f"raw-{i}.jsonl")
                converted_path = os.path.join(staging, f"converted-{i}.jsonl")
                pk_client.download_to(job, raw)
                mg_client.renew(lease.lease_id)  # renew after download, before normalize/upload
                _, sk = convert_portkey_export(raw, converted_path, import_context=ctx)
                skipped += sk
                s, f = push(converted_path, push_token, base_url=ingest_base_url)
                pushed += s
                failed += f
                mg_client.renew(lease.lease_id)  # renew after each upload
            if failed:
                mg_client.abandon(lease.lease_id)
                return SyncOutcome(
                    "failed", f"{failed} row(s) failed to upload; lease released, will retry next run.",
                    1, pushed=pushed, failed=failed, skipped=skipped,
                )
            mg_client.complete(lease.lease_id)
            return SyncOutcome(
                "completed",
                f"Imported window {lease.window_start}..{lease.window_end}: "
                f"pushed {pushed} row(s), skipped {skipped}, {failed} failed.",
                0, pushed=pushed, failed=failed, skipped=skipped,
            )
    except LeaseLostError as exc:
        return SyncOutcome("failed", f"Lease lost mid-run ({exc}); relying on server expiry.", 1)
    except (PortkeyExportError, MeterGraphSyncError, OSError) as exc:
        mg_client.abandon(lease.lease_id)
        return SyncOutcome("failed", f"Sync failed: {exc}; lease released.", 1)


def _collect_export_jobs(pk_client, lease, mg_client, *, sleep, poll_interval, max_poll_seconds):
    full = pk_client.submit_export(window_start=lease.window_start, window_end=lease.window_end)
    full = _poll_all([full], pk_client, mg_client, lease.lease_id, sleep, poll_interval, max_poll_seconds)[0]
    if full.record_count is not None and full.record_count > VOLUME_SPLIT_THRESHOLD:
        sub_windows = split_window(TimeWindow(start=lease.window_start, end=lease.window_end))
        sub_jobs = [pk_client.submit_export(window_start=w.start, window_end=w.end) for w in sub_windows]
        return _poll_all(sub_jobs, pk_client, mg_client, lease.lease_id, sleep, poll_interval, max_poll_seconds)
    return [full]


def _poll_all(jobs, pk_client, mg_client, lease_id, sleep, poll_interval, max_poll_seconds):
    current = list(jobs)
    elapsed = 0.0
    while not all(j.is_terminal for j in current):
        sleep(poll_interval)
        elapsed += poll_interval
        mg_client.renew(lease_id)  # keep the lease alive across the whole poll loop
        current = [pk_client.get_job(j.job_id) if not j.is_terminal else j for j in current]
        if elapsed >= max_poll_seconds:
            raise PortkeyExportError(f"Portkey export polling exceeded {max_poll_seconds}s safety cap")
    failures = [j.job_id for j in current if not j.is_success]
    if failures:
        raise PortkeyExportError(f"Portkey export job(s) failed: {', '.join(failures)}")
    return current
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_portkey_sync.py -v`
Expected: PASS — all tests (happy path, split path, busy/caught_up, push failure, portkey error, lease-lost).

- [ ] **Step 5: Commit**

```bash
git add src/metergraphrelay/portkey_sync.py tests/test_portkey_sync.py
git commit -m "feat(sync): add resumable Portkey sync orchestrator with volume split and lease lifecycle"
```

---

### Task 6: CLI wiring, credentials, and env isolation

**Files:**
- Modify: `src/metergraphrelay/config.py` (add `portkey` credential spec)
- Modify: `tests/conftest.py` (register new non-credential env vars)
- Modify: `src/metergraphrelay/cli.py` (optional positional + API-mode args + dispatch)
- Test: `tests/test_cli.py` (append API-mode dispatch/validation tests)

**Interfaces:**
- Consumes: `run_portkey_sync`/`SyncOutcome` (Task 5); `MeterGraphSyncClient`/`MeterGraphSyncError` (Task 3); `PortkeyExportClient`/`PortkeyExportError` (Task 4); `push.DEFAULT_INGEST_URL`; existing `require_credentials`/`ConfigError`/`_config_error`.
- Produces: `_run_sync_portkey_api(args) -> int`; a `sync portkey` parser where `export_file` is optional and API-mode flags exist.

- [ ] **Step 1: Add the `portkey` credential spec** in `config.py`

Edit `CREDENTIAL_SPECS` to add one line:
```python
    "portkey": ["PORTKEY_API_KEY"],
```
(conftest's `ENV_VARS_READ_BY_CLI` derives credentials from `CREDENTIAL_SPECS`, so `PORTKEY_API_KEY` is auto-isolated.)

- [ ] **Step 2: Register non-credential env vars** in `tests/conftest.py`

Change the explicit set so the new optional vars are cleared per test, exactly like the existing ones:
```python
ENV_VARS_READ_BY_CLI = sorted(
    {name for names in CREDENTIAL_SPECS.values() for name in names}
    | {"METERGRAPH_INGEST_URL", "LANGFUSE_BASE_URL", "PORTKEY_WORKSPACE", "PORTKEY_BASE_URL"}
)
```

- [ ] **Step 3: Write the failing CLI tests** (append to `tests/test_cli.py`)

```python
from metergraphrelay.portkey_sync import SyncOutcome  # add to imports


def test_sync_portkey_manual_mode_still_dispatches_to_local_converter(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("METERGRAPH_APP_TOKEN=tok-123\n")
    export_file = tmp_path / "export.jsonl"
    export_file.write_text("")

    with patch("metergraphrelay.cli._run_sync_portkey", return_value=0) as manual, patch(
        "metergraphrelay.cli._run_sync_portkey_api"
    ) as api:
        exit_code = main(["sync", "portkey", str(export_file), "--env-file", str(env_file)])

    assert exit_code == 0
    manual.assert_called_once()
    api.assert_not_called()


def test_sync_portkey_no_export_file_dispatches_to_api_mode(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("PORTKEY_API_KEY=pk-1\nMETERGRAPH_APP_TOKEN=tok-123\n")

    with patch("metergraphrelay.cli._run_sync_portkey_api", return_value=0) as api, patch(
        "metergraphrelay.cli._run_sync_portkey"
    ) as manual:
        exit_code = main(
            ["sync", "portkey", "--source-scope", "ws-acme", "--env-file", str(env_file)]
        )

    assert exit_code == 0
    api.assert_called_once()
    manual.assert_not_called()


def test_sync_portkey_api_missing_portkey_credential_returns_error(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("METERGRAPH_APP_TOKEN=tok-123\n")

    exit_code = main(
        ["sync", "portkey", "--source-scope", "ws-acme", "--env-file", str(env_file)]
    )

    assert exit_code == 1
    assert "PORTKEY_API_KEY" in capsys.readouterr().err


def test_sync_portkey_api_missing_source_scope_returns_error(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("PORTKEY_API_KEY=pk-1\nMETERGRAPH_APP_TOKEN=tok-123\n")

    exit_code = main(["sync", "portkey", "--env-file", str(env_file)])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "source" in err.lower() or "PORTKEY_WORKSPACE" in err


def test_sync_portkey_api_source_scope_from_env(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "PORTKEY_API_KEY=pk-1\nMETERGRAPH_APP_TOKEN=tok-123\nPORTKEY_WORKSPACE=ws-from-env\n"
    )

    with patch(
        "metergraphrelay.cli.run_portkey_sync",
        return_value=SyncOutcome("completed", "done", 0),
    ) as run:
        exit_code = main(["sync", "portkey", "--env-file", str(env_file)])

    assert exit_code == 0
    assert run.call_args.kwargs["source_scope"] == "ws-from-env"


def test_sync_portkey_api_passes_initial_since_and_max_window(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("PORTKEY_API_KEY=pk-1\nMETERGRAPH_APP_TOKEN=tok-123\n")

    with patch(
        "metergraphrelay.cli.run_portkey_sync",
        return_value=SyncOutcome("caught_up", "caught up", 0),
    ) as run:
        main(
            [
                "sync", "portkey", "--source-scope", "ws-acme",
                "--initial-since", "2026-08-01T00:00:00+00:00",
                "--env-file", str(env_file),
            ]
        )

    kwargs = run.call_args.kwargs
    assert kwargs["initial_since"] == "2026-08-01T00:00:00+00:00"
    assert kwargs["max_window_seconds"] == 3600


def test_sync_portkey_api_rejects_max_window_over_3600(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("PORTKEY_API_KEY=pk-1\nMETERGRAPH_APP_TOKEN=tok-123\n")

    exit_code = main(
        [
            "sync", "portkey", "--source-scope", "ws-acme",
            "--max-window-seconds", "7200", "--env-file", str(env_file),
        ]
    )

    assert exit_code == 1
    assert "3600" in capsys.readouterr().err


def test_sync_portkey_output_flag_rejected_in_api_mode(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("PORTKEY_API_KEY=pk-1\nMETERGRAPH_APP_TOKEN=tok-123\n")

    exit_code = main(
        [
            "sync", "portkey", "--source-scope", "ws-acme",
            "--output", "converted.jsonl", "--env-file", str(env_file),
        ]
    )

    assert exit_code == 1
    assert "--output" in capsys.readouterr().err


def test_sync_portkey_api_error_returns_clean_exit(tmp_path, capsys):
    from metergraphrelay.metergraph_sync import MeterGraphSyncError

    env_file = tmp_path / ".env"
    env_file.write_text("PORTKEY_API_KEY=pk-1\nMETERGRAPH_APP_TOKEN=tok-123\n")

    with patch(
        "metergraphrelay.cli.run_portkey_sync",
        side_effect=MeterGraphSyncError("acquire failed: HTTP 500 err"),
    ):
        exit_code = main(["sync", "portkey", "--source-scope", "ws-acme", "--env-file", str(env_file)])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert err.startswith("Error: ")
    assert "Traceback" not in err


def test_sync_portkey_api_busy_prints_and_exits_zero(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("PORTKEY_API_KEY=pk-1\nMETERGRAPH_APP_TOKEN=tok-123\n")

    with patch(
        "metergraphrelay.cli.run_portkey_sync",
        return_value=SyncOutcome("busy", "Another sync holds the lease; retry at X.", 0),
    ):
        exit_code = main(["sync", "portkey", "--source-scope", "ws-acme", "--env-file", str(env_file)])

    assert exit_code == 0
    assert "retry at" in capsys.readouterr().out.lower()


def test_sync_portkey_help_documents_api_mode(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["sync", "portkey", "--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    for expected in [
        "--source-scope", "--initial-since", "--max-window-seconds",
        "PORTKEY_API_KEY", "workspace",
    ]:
        assert expected in help_text, f"missing {expected!r} in --help output"
```

- [ ] **Step 4: Run the new CLI tests to verify they fail**

Run: `.venv/bin/pytest tests/test_cli.py -v -k sync_portkey`
Expected: FAIL — API-mode flags/behavior not present yet (and existing `sync portkey` tests in `tests/providers/test_portkey.py` still pass because manual dispatch is unchanged).

- [ ] **Step 5: Wire the CLI in `cli.py`**

- Add imports near the top:
```python
from .metergraph_sync import MeterGraphSyncClient, MeterGraphSyncError
from .portkey_sync import run_portkey_sync
from .providers.portkey_export import PortkeyExportClient, PortkeyExportError
from .push import DEFAULT_INGEST_URL, push_file
```
- In `build_parser()`, change the `sync_portkey_parser` positional to optional and add API-mode flags (keep the existing `--output`/`--env-file`). Update the `description=`/`help=` to also state the API cron mode, the `PORTKEY_API_KEY` requirement, the one-workspace-per-app assumption, and that `source_scope` is the Portkey workspace id (not a secret):
```python
sync_portkey_parser.add_argument(
    "export_file", metavar="EXPORT_FILE", nargs="?", default=None,
    help="Local Portkey JSONL export (manual mode). Omit to use Portkey API cron mode.",
)
sync_portkey_parser.add_argument(
    "--source-scope", default=None,
    help="Portkey workspace identifier for API cron mode (falls back to $PORTKEY_WORKSPACE). "
         "This is the stable workspace id, not a secret. One workspace per MeterGraph app (MVP).",
)
sync_portkey_parser.add_argument(
    "--initial-since", default=None,
    help="Aware ISO 8601 timestamp seeding the first sync window (API mode). Required only on the "
         "first run; the server ignores it once state exists, so cron may pass it every run.",
)
sync_portkey_parser.add_argument(
    "--max-window-seconds", type=int, default=None,
    help="Maximum logical window length in seconds (API mode, <=3600). (default: 3600)",
)
```
  (Existing `--output` help gains "(manual mode only)".)
- Replace the dispatch in `main()`:
```python
    if args.command == "sync" and args.provider == "portkey":
        if args.export_file is not None:
            return _run_sync_portkey(args)
        return _run_sync_portkey_api(args)
```
- Add `_run_sync_portkey_api`:
```python
def _run_sync_portkey_api(args: argparse.Namespace) -> int:
    if args.output is not None:
        print("Error: --output is only valid with a local EXPORT_FILE (manual mode).", file=sys.stderr)
        return 1
    try:
        portkey_creds = require_credentials("portkey", args.env_file)
        push_creds = require_credentials("push", args.env_file)
    except ConfigError as exc:
        return _config_error(exc)
    # require_credentials() has now loaded the env file; non-secret config can be read.
    source_scope = args.source_scope or os.environ.get("PORTKEY_WORKSPACE")
    if not source_scope:
        return _config_error(
            ConfigError("source scope not set. Pass --source-scope or set PORTKEY_WORKSPACE "
                        "(the Portkey workspace id; not a secret).")
        )
    max_window = args.max_window_seconds if args.max_window_seconds is not None else 3600
    if max_window <= 0 or max_window > 3600:
        return _config_error(ConfigError("--max-window-seconds must be between 1 and 3600."))
    ingest_base = os.environ.get("METERGRAPH_INGEST_URL")
    portkey_base = os.environ.get("PORTKEY_BASE_URL")
    mg_client = MeterGraphSyncClient(ingest_base or DEFAULT_INGEST_URL, push_creds["METERGRAPH_APP_TOKEN"])
    pk_kwargs = {"workspace": source_scope}
    if portkey_base:
        pk_kwargs["base_url"] = portkey_base
    pk_client = PortkeyExportClient(portkey_creds["PORTKEY_API_KEY"], **pk_kwargs)
    try:
        outcome = run_portkey_sync(
            mg_client=mg_client, pk_client=pk_client, source_scope=source_scope,
            initial_since=args.initial_since, max_window_seconds=max_window,
            push_token=push_creds["METERGRAPH_APP_TOKEN"], ingest_base_url=ingest_base,
        )
    except (MeterGraphSyncError, PortkeyExportError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(outcome.detail)
    return outcome.exit_code
```

- [ ] **Step 6: Run the full CLI and provider suites to verify GREEN**

Run: `.venv/bin/pytest tests/test_cli.py tests/providers/test_portkey.py -v`
Expected: PASS — new API-mode tests **and** all pre-existing manual-mode tests.

- [ ] **Step 7: Commit**

```bash
git add src/metergraphrelay/config.py src/metergraphrelay/cli.py tests/conftest.py tests/test_cli.py
git commit -m "feat(cli): add Portkey API cron mode to sync portkey (optional export file)"
```

---

### Task 7: Docs, version bump, and full verification

**Files:**
- Modify: `README.md`
- Modify: `.env.example`
- Modify: `src/metergraphrelay/__init__.py` (version bump)
- Modify: `pyproject.toml` (version bump)

**Interfaces:** none (documentation + release metadata).

- [ ] **Step 1: Add a "Sync from Portkey (API cron mode)" subsection to `README.md`**, after the existing "## Sync from Portkey" section, in the same prose+fenced-example style. It must cover, each as a short paragraph or bullet:
  - This mode contacts the Portkey Logs Export API (unlike manual mode) and requires `PORTKEY_API_KEY` in addition to `METERGRAPH_APP_TOKEN`.
  - One Portkey workspace per MeterGraph app (MVP); `--source-scope` (or `$PORTKEY_WORKSPACE`) is the stable workspace id, not a secret.
  - Fixed 1-hour windows with a server-owned 5-minute overlap and a 15-minute renewable lease; the relay keeps no local checkpoint — resume state lives on the MeterGraph server.
  - `--initial-since` semantics: required only on the first run; the server ignores it afterward, so cron may pass it every run.
  - `busy` and `caught_up` are clean no-op exits (exit 0); a handled failure releases the lease and exits nonzero; a crash relies on server lease expiry.
  - A cron example, verbatim so the README test can assert it:
    ```
    # Hourly customer-managed cron (no local state, safe to overlap runs):
    0 * * * * metergraphrelay sync portkey --source-scope ws-acme --initial-since 2026-08-01T00:00:00+00:00
    ```
  - Pointer to `metergraphrelay sync portkey --help`.
  - Update the provider-status paragraph to mention the new API cron mode alongside manual mode.

- [ ] **Step 2: Add a README parse/assert test** (append to `tests/test_cli.py`) so the documented cron command stays valid:
```python
def test_readme_portkey_cron_example_parses():
    readme = (Path(__file__).parent.parent / "README.md").read_text()
    assert (
        "metergraphrelay sync portkey --source-scope ws-acme "
        "--initial-since 2026-08-01T00:00:00+00:00"
    ) in readme
    build_parser().parse_args(
        ["sync", "portkey", "--source-scope", "ws-acme",
         "--initial-since", "2026-08-01T00:00:00+00:00"]
    )
```

- [ ] **Step 3: Document the new env vars in `.env.example`**, appended after the existing entries:
```
# Portkey API cron mode (metergraphrelay sync portkey with no export file):
PORTKEY_API_KEY=pk-your-portkey-key-here
# PORTKEY_WORKSPACE=ws-your-workspace-id   # or pass --source-scope; the stable workspace id, not a secret
# PORTKEY_BASE_URL=https://api.portkey.ai  # optional; override the Portkey API base URL
```

- [ ] **Step 4: Bump the version (two-file convention, kept in sync)**

The repo publishes to PyPI on GitHub Release and keeps the version in exactly two places that must match. Bump minor for this feature:
- `src/metergraphrelay/__init__.py`: `__version__ = "0.4.0"`
- `pyproject.toml`: `version = "0.4.0"`

(No CHANGELOG file exists in this repo; release notes live in the GitHub Release, authored at release time — nothing to edit here.)

- [ ] **Step 5: Full-suite verification**

Run: `.venv/bin/pytest -v`
Expected: every test passes across `tests/test_window.py`, `tests/test_metergraph_sync.py`, `tests/providers/test_portkey_export.py`, `tests/test_portkey_sync.py`, `tests/providers/test_portkey.py`, `tests/test_cli.py`, `tests/test_config.py`, `tests/test_demo.py`, `tests/test_push.py`, `tests/providers/test_openai.py`, `tests/providers/test_langfuse.py` — zero failures, zero errors.

Run: `.venv/bin/python -m metergraphrelay.cli sync portkey --help`
Expected: help lists `EXPORT_FILE` as optional plus `--source-scope`, `--initial-since`, `--max-window-seconds`, and names `PORTKEY_API_KEY`.

Run: `.venv/bin/python -m metergraphrelay.cli sync portkey`
Expected: clean `Error: ...` about missing `PORTKEY_API_KEY` (or source scope), exit 1, no traceback.

Run: `.venv/bin/python -m build`
Expected: builds `dist/metergraphrelay-0.4.0.tar.gz` and `dist/metergraphrelay-0.4.0-py3-none-any.whl`.

Run: `git status --short`
Expected: only the intended files changed; `metergraphrelay.cdx.json` and `requirements.txt` remain untracked (`??`) and unmodified.

- [ ] **Step 6: Commit**

```bash
git add README.md .env.example src/metergraphrelay/__init__.py pyproject.toml tests/test_cli.py
git commit -m "docs: document Portkey API cron mode; chore: bump version to 0.4.0"
```

---

## Self-Review

**1. Spec coverage** (each Approved-Contract item → task):

| Contract requirement | Task |
|---|---|
| `acquire` body/responses (201/200/409), Bearer auth | Task 3 |
| `renew`/`complete`/`abandon`(DELETE)/`state` endpoints | Task 3 |
| `import_source`/`import_source_scope`/`import_event_id` dedup fields | Task 2 (fields), Task 5 (applied) |
| Preserve manual `sync portkey EXPORT_FILE` | Tasks 2, 6 (optional positional; existing tests green) |
| API mode without positional export file | Task 6 |
| One workspace per app; `source_scope` stable, not secret | Tasks 5, 6 (CLI/help), 7 (README) |
| No local checkpoint files (server-only resume) | Task 5 (TemporaryDirectory staging only) |
| `initial_since` first-run-only, cron may send every run | Tasks 5, 6, 7 |
| Fixed 1h window / 5-min overlap / 15-min lease (server-owned) | Task 5 consumes server window; docs Task 7 |
| Renew during submit/poll, download, normalize/upload | Task 5 (`_poll_all` + explicit renews) |
| Handled failure releases lease + exits nonzero; crash → expiry | Task 5 (`abandon`), tests cover |
| busy/caught_up clean no-op exits (0) | Tasks 5, 6 |
| Complete only after all uploads succeed | Task 5 (`if failed: abandon` before `complete`) |
| Submit→poll→download→normalize→upload | Task 5 |
| >50k → one split into 10 intervals w/ 1s overlap, poll together | Task 1 (`split_window`), Task 5 (threshold+orchestration) |
| Isolate window/orchestration from CLI; Portkey only | Tasks 1/5 (modules), Task 6 (thin CLI) |
| Actionable errors, safe secret handling, consistent timeout | Tasks 3/4/5/6 |
| README + `--help` updates | Tasks 6 (help), 7 (README/.env) |
| Tests RED before production | Every task's Step 2/4 ordering |
| Unit + CLI + orchestration + fake e2e tests | Tasks 1–5 unit/orchestration, 5 e2e, 6 CLI |
| Versioning/release per repo convention | Task 7 (two-file bump; no CHANGELOG exists) |

No gaps found.

**2. Placeholder scan:** All test and implementation steps contain concrete code; no "TBD"/"add error handling"/"similar to Task N". The one deliberately-unverified area (Portkey wire format) is explicitly flagged with named constants and a quarantine boundary in Task 4 — not a silent placeholder.

**3. Type consistency check:**
- `MeterGraphSyncClient.acquire(...) -> AcquireResult`; `.lease: AcquiredLease | None`; fields `lease_id`/`window_start`/`window_end`/`lease_expires_at`/`checkpoint_version` used identically in Tasks 3 and 5. ✔
- `PortkeyExportJob(job_id, status, record_count, download_token)` with `.is_terminal`/`.is_success`; used identically in Tasks 4 and 5 (and in the Task 5 fakes). ✔
- `TimeWindow(start, end)` and `split_window(window)` names/fields match between Tasks 1 and 5. ✔
- `ImportContext(source, source_scope)` and `convert_portkey_export(in, out, *, import_context=...)` match between Tasks 2 and 5. ✔
- `run_portkey_sync(...) -> SyncOutcome(status, detail, exit_code, ...)`; consumed by CLI in Task 6 via `.detail`/`.exit_code`. ✔
- `PORTKEY_SOURCE="portkey"` used for both acquire `source` and `import_source`. ✔
- CLI reuses `push.DEFAULT_INGEST_URL` and `os.environ["METERGRAPH_INGEST_URL"]` — same base-URL resolution as existing `push`/`sync` handlers. ✔

No drift found.

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-08-19-portkey-cron-sync-plan.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?** (Note: Task 4's Portkey wire format must be confirmed against Portkey's Logs Export API docs before its production HTTP is written — flagged in-task.)
