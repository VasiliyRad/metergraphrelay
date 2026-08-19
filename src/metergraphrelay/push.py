from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Callable

DEFAULT_INGEST_URL = "https://d2xus7mp8zdv6t.cloudfront.net"


def push_file(
    file_path: str,
    token: str,
    base_url: str | None = None,
    *,
    on_progress: Callable[[], None] | None = None,
) -> tuple[int, int]:
    """Upload each JSONL row to the ingest endpoint, returning (succeeded, failed).

    ``on_progress``, if given, is invoked once per processed row (one HTTP request
    per row means a large file can outlive a lease, so a caller can use this hook to
    renew mid-upload). It fires for every non-blank line — successful, failed, or
    malformed — and any exception it raises is allowed to propagate (a lost lease
    must abort the upload, not be swallowed). It defaults to ``None`` so existing
    callers and manual mode are unchanged.
    """
    url = f"{(base_url or DEFAULT_INGEST_URL).rstrip('/')}/v1/ingest"
    succeeded = 0
    failed = 0
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
            body = json.dumps({"schema_version": 1, "rows": [row], "meta": {}}).encode()
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
                with urllib.request.urlopen(request, timeout=10) as response:
                    if response.status == 202:
                        succeeded += 1
                    else:
                        failed += 1
                        print(
                            f"Warning: unexpected status {response.status} pushing a row",
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
