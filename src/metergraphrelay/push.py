from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Callable

DEFAULT_INGEST_URL = "https://d2xus7mp8zdv6t.cloudfront.net"

# The server (metergraph_app.api.ingestion) rejects a request over 5000 rows
# or an 8MB decompressed body. Kept well under both so a batch never needs
# retrying at a smaller size.
MAX_ROWS_PER_BATCH = 2000
MAX_BATCH_BYTES = 6 * 1024 * 1024


def push_file(
    file_path: str,
    token: str,
    base_url: str | None = None,
    *,
    on_progress: Callable[[], None] | None = None,
    max_rows_per_batch: int = MAX_ROWS_PER_BATCH,
    max_batch_bytes: int = MAX_BATCH_BYTES,
) -> tuple[int, int]:
    """Upload every JSONL row to the ingest endpoint in batches, returning
    (succeeded, failed).

    Rows are grouped into one POST per batch, bounded by ``max_rows_per_batch``
    and ``max_batch_bytes``, rather than one request per row -- the server
    rejects a request over either bound. A batch's ``accepted``/``ignored``
    counts come from the server's own response, not just its HTTP status: a
    202 can still ignore some rows (e.g. a content-capture policy), and those
    count as failed, not succeeded.

    ``on_progress``, if given, still fires once per processed row --
    successful, failed, or malformed -- regardless of batching: a caller
    renewing a lease mid-upload needs that per-row cadence, not a per-batch
    one. Any exception it raises propagates (a lost lease must abort the
    upload, not be swallowed). It defaults to ``None`` so existing callers and
    manual mode are unchanged.
    """
    url = f"{(base_url or DEFAULT_INGEST_URL).rstrip('/')}/v1/ingest"
    succeeded = 0
    failed = 0
    batch: list[dict] = []
    batch_bytes = 0

    def flush() -> None:
        nonlocal succeeded, failed, batch, batch_bytes
        if not batch:
            return
        body = json.dumps({"schema_version": 1, "rows": batch, "meta": {}}).encode()
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status == 202:
                    accepted = len(batch)
                    try:
                        accepted = json.loads(response.read()).get("accepted", accepted)
                    except json.JSONDecodeError:
                        pass
                    succeeded += accepted
                    failed += len(batch) - accepted
                else:
                    failed += len(batch)
                    print(
                        f"Warning: unexpected status {response.status} pushing "
                        f"a batch of {len(batch)} row(s)",
                        file=sys.stderr,
                    )
        except urllib.error.HTTPError as exc:
            failed += len(batch)
            print(
                f"Warning: push failed for a batch of {len(batch)} row(s): "
                f"HTTP {exc.code} {exc.reason}",
                file=sys.stderr,
            )
        except urllib.error.URLError as exc:
            failed += len(batch)
            print(
                f"Warning: push failed for a batch of {len(batch)} row(s): {exc.reason}",
                file=sys.stderr,
            )
        batch = []
        batch_bytes = 0

    with open(file_path) as f:
        for line_number, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            if on_progress is not None:
                on_progress()
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                failed += 1
                print(
                    f"Warning: skipping malformed JSON on line {line_number}: {exc}",
                    file=sys.stderr,
                )
                continue
            encoded_size = len(json.dumps(row).encode())
            if batch and (
                len(batch) >= max_rows_per_batch
                or batch_bytes + encoded_size > max_batch_bytes
            ):
                flush()
            batch.append(row)
            batch_bytes += encoded_size
    flush()

    return succeeded, failed
