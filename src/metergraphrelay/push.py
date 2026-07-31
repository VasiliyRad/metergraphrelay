from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

DEFAULT_INGEST_URL = "https://d2xus7mp8zdv6t.cloudfront.net"


def push_file(
    file_path: str, token: str, base_url: str | None = None
) -> tuple[int, int]:
    url = f"{(base_url or DEFAULT_INGEST_URL).rstrip('/')}/v1/ingest"
    succeeded = 0
    failed = 0
    with open(file_path) as f:
        for line_number, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
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
