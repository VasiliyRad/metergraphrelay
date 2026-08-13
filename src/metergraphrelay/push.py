from __future__ import annotations

import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
import uuid

from .contract import IMPORT_CONTRACT_VERSION

DEFAULT_INGEST_URL = "https://d2xus7mp8zdv6t.cloudfront.net"
IMPORT_BATCH_ROWS = 500
IMPORT_BATCH_BYTES = 4 * 1024 * 1024
IMPORT_ENVELOPE_RESERVE = 2048


def _request(url: str, token: str, payload: dict) -> int:
    body = json.dumps(payload, separators=(",", ":")).encode()
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status


def _import_batches(rows: list[dict]) -> list[list[dict]]:
    batches: list[list[dict]] = []
    current: list[dict] = []
    current_bytes = 0
    for row in rows:
        size = len(json.dumps(row, separators=(",", ":")).encode()) + 1
        if size > IMPORT_BATCH_BYTES - IMPORT_ENVELOPE_RESERVE:
            raise ValueError("one import row exceeds the 4 MiB batch limit")
        if current and (
            len(current) >= IMPORT_BATCH_ROWS
            or current_bytes + size > IMPORT_BATCH_BYTES - IMPORT_ENVELOPE_RESERVE
        ):
            batches.append(current)
            current, current_bytes = [], 0
        current.append(row)
        current_bytes += size
    if current:
        batches.append(current)
    return batches


def _push_import_rows(url: str, token: str, rows: list[dict]) -> tuple[int, int]:
    identities = {
        (
            row.get("import_source"),
            row.get("import_source_scope"),
        )
        for row in rows
    }
    if len(identities) != 1:
        print(
            "Warning: an import file cannot mix providers or source scopes.",
            file=sys.stderr,
        )
        return 0, len(rows)
    source, source_scope = next(iter(identities))
    if source not in {"openai", "langfuse", "portkey"} or not source_scope:
        print("Warning: import provenance is incomplete.", file=sys.stderr)
        return 0, len(rows)
    if any(not row.get("import_event_id") for row in rows):
        print("Warning: every import row requires import_event_id.", file=sys.stderr)
        return 0, len(rows)
    file_digest = hashlib.sha256(
        b"\n".join(
            json.dumps(row, separators=(",", ":"), sort_keys=True).encode()
            for row in rows
        )
    ).hexdigest()
    token_fingerprint = hashlib.sha256(token.encode()).hexdigest()
    run_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            "metergraph-log-import-v1:"
            f"{token_fingerprint}:{source}:{source_scope}:{file_digest}",
        )
    )
    batches = _import_batches(rows)
    succeeded = failed = 0
    for index, batch in enumerate(batches):
        payload = {
            "schema_version": 1,
            "rows": batch,
            "meta": {
                "log_import": {
                    "contract_version": IMPORT_CONTRACT_VERSION,
                    "run_id": run_id,
                    "source": source,
                    "source_scope": source_scope,
                    "batch_index": index,
                    "final": index == len(batches) - 1,
                }
            },
        }
        for attempt in range(3):
            try:
                status = _request(url, token, payload)
                if status == 202:
                    succeeded += len(batch)
                else:
                    failed += len(batch)
                    print(
                        f"Warning: unexpected status {status} pushing import batch {index}.",
                        file=sys.stderr,
                    )
                break
            except (urllib.error.HTTPError, urllib.error.URLError) as exc:
                if attempt < 2 and not (
                    isinstance(exc, urllib.error.HTTPError) and 400 <= exc.code < 500
                ):
                    time.sleep(0.25 * (2**attempt))
                    continue
                failed += len(batch)
                detail = (
                    f"HTTP {exc.code} {exc.reason}"
                    if isinstance(exc, urllib.error.HTTPError)
                    else str(exc.reason)
                )
                print(
                    f"Warning: push failed for import batch {index}: {detail}",
                    file=sys.stderr,
                )
                break
    return succeeded, failed


def push_file(
    file_path: str, token: str, base_url: str | None = None
) -> tuple[int, int]:
    url = f"{(base_url or DEFAULT_INGEST_URL).rstrip('/')}/v1/ingest"
    rows: list[dict] = []
    malformed = 0
    with open(file_path) as f:
        for line_number, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                malformed += 1
                print(
                    f"Warning: skipping malformed JSON on line {line_number}: {exc}",
                    file=sys.stderr,
                )
                continue
            if not isinstance(row, dict):
                malformed += 1
                print(
                    f"Warning: skipping non-object JSON on line {line_number}.",
                    file=sys.stderr,
                )
                continue
            rows.append(row)
    imported = [bool(row.get("import_source")) for row in rows]
    if any(imported):
        if not all(imported):
            print(
                "Warning: a push file cannot mix legacy rows and versioned import rows.",
                file=sys.stderr,
            )
            return 0, len(rows) + malformed
        succeeded, failed = _push_import_rows(url, token, rows)
        return succeeded, failed + malformed

    succeeded = 0
    failed = malformed
    for row in rows:
        try:
            status = _request(
                url, token, {"schema_version": 1, "rows": [row], "meta": {}}
            )
            if status == 202:
                succeeded += 1
            else:
                failed += 1
                print(
                    f"Warning: unexpected status {status} pushing a row",
                    file=sys.stderr,
                )
        except urllib.error.HTTPError as exc:
            failed += 1
            print(
                f"Warning: push failed for a row: HTTP {exc.code} {exc.reason}",
                file=sys.stderr,
            )
        except urllib.error.URLError as exc:
            failed += 1
            print(f"Warning: push failed for a row: {exc.reason}", file=sys.stderr)
    return succeeded, failed
