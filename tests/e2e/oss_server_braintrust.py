"""Latest relay Braintrust pull -> latest OSS server contract smoke test.

Drives the real ``pull_braintrust`` loop against a stubbed ``/btql`` page, so no
Braintrust credential is needed, then pushes the resulting JSONL to a running
OSS server and reads it back through ``/v1/calls``. What survives that round
trip is the actual relay -> server contract; unit tests only prove what the
relay writes to disk.

Three things this is here to catch:

* **The token convention.** Braintrust's ``prompt_tokens`` is a TOTAL that
  already includes cache reads and writes, and metergraph's convention is the
  same, so the relay must NOT add the cache details back in -- unlike the
  Langfuse provider, whose flattened shape requires exactly that. A regression
  in either direction shows up here as ``input_tokens`` 170 instead of 100.
* **Historical timestamps.** A span's ``created`` must reach the server intact
  rather than being replaced with ingest time.
* **Derived latency.** ``metrics.start``/``metrics.end`` are epoch seconds; the
  server stores an integer millisecond latency.

Set ``BRAINTRUST_E2E_PROJECT`` (with ``BRAINTRUST_API_KEY``) to additionally run
one real query against a live Braintrust workspace. That part is skipped by
default and never runs in CI.
"""

from __future__ import annotations

import json
import os
import tempfile
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from metergraphrelay.providers import braintrust
from metergraphrelay.providers.braintrust import pull_braintrust
from metergraphrelay.push import push_file

HISTORICAL_TIMESTAMP = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
ROUTE_PREFIX = "relay.braintrust-e2e"
# Deliberately a "Z" designator rather than "+00:00": Python 3.10's
# fromisoformat rejects "Z", and the OSS server supports 3.10, so an
# un-normalized timestamp would silently become ingest time here.
CREATED = "2026-08-10T12:00:00Z"

# prompt_tokens is the inclusive total: 30 uncached + 60 cache read + 10 cache
# write. The relay must forward 100, not 100 + 60 + 10.
PROMPT_TOKENS = 100
CACHE_READ_TOKENS = 60
CACHE_WRITE_TOKENS = 10
COMPLETION_TOKENS = 20
REASONING_TOKENS = 5
LATENCY_MS = 1500


def _span(span_id: str, *, error: object = None) -> dict:
    """One LLM span shaped the way /btql returns it for shape => 'spans'."""
    return {
        "id": span_id,
        "created": CREATED,
        "span_id": span_id,
        "root_span_id": "braintrust-e2e-trace-1",
        "span_parents": ["braintrust-e2e-trace-1"],
        "span_attributes": {"name": "OpenAI Chat Completion", "type": "llm"},
        "input": [{"role": "user", "content": "braintrust e2e smoke"}],
        "output": {"role": "assistant", "content": "ack"},
        "error": error,
        "metadata": {"model": "gpt-4o-mini", "provider": "openai"},
        "metrics": {
            "start": HISTORICAL_TIMESTAMP.timestamp(),
            "end": HISTORICAL_TIMESTAMP.timestamp() + LATENCY_MS / 1000,
            "prompt_tokens": PROMPT_TOKENS,
            "completion_tokens": COMPLETION_TOKENS,
            "prompt_cached_tokens": CACHE_READ_TOKENS,
            "prompt_cache_creation_tokens": CACHE_WRITE_TOKENS,
            "completion_reasoning_tokens": REASONING_TOKENS,
        },
        "tags": ["e2e"],
        "project_id": "braintrust-e2e-project",
        "estimated_cost": 0.00042,
    }


OK_SPAN_ID = "braintrust-e2e-ok"
ERROR_SPAN_ID = "braintrust-e2e-error"
ERROR_MESSAGE = "upstream rate limited"


def _fetch_one_page(base_url, *, api_key, query):
    """Stand in for /btql, asserting the query still carries the LLM filter."""
    assert "shape => 'spans'" in query, query
    assert "span_attributes.type = 'llm'" in query, query
    spans = [
        _span(OK_SPAN_ID),
        _span(ERROR_SPAN_ID, error={"message": ERROR_MESSAGE, "code": 429}),
    ]
    return spans, None


def _read_back(base_url: str, token: str, route: str) -> dict[str, dict]:
    query = urllib.parse.urlencode({"route": route})
    request = urllib.request.Request(
        f"{base_url}/v1/calls?{query}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.load(response)
    return {
        item["request_id"]: item
        for item in payload["items"]
        if item["route"] == route
    }


def _check_pull_and_push(base_url: str, token: str) -> None:
    route = f"{ROUTE_PREFIX}.{uuid.uuid4().hex}"

    with tempfile.TemporaryDirectory() as temporary_directory:
        output = Path(temporary_directory) / "braintrust.jsonl"
        with patch.object(braintrust, "fetch_spans_page", _fetch_one_page):
            imported, skipped = pull_braintrust(
                base_url="https://api.braintrust.dev",
                api_key="not-used-by-the-stub",
                projects=["braintrust-e2e-project"],
                count=10,
                since="2026-08-01T00:00:00+00:00",
                until="2026-08-11T00:00:00+00:00",
                route=route,
                output_path=str(output),
            )
        assert (imported, skipped) == (2, 0), (imported, skipped)

        written = [json.loads(line) for line in output.read_text().splitlines()]
        assert len(written) == 2, written
        # The relay's own view, before the server sees it.
        assert written[0]["ts"] == "2026-08-10T12:00:00+00:00", written[0]["ts"]
        assert written[0]["input_tokens"] == PROMPT_TOKENS, written[0]

        assert push_file(str(output), token=token, base_url=base_url) == (2, 0)

    items = _read_back(base_url, token, route)
    assert set(items) == {OK_SPAN_ID, ERROR_SPAN_ID}, sorted(items)

    ok = items[OK_SPAN_ID]
    observed = datetime.fromisoformat(ok["ts"]).astimezone(timezone.utc)
    assert observed == HISTORICAL_TIMESTAMP, ok

    assert ok["provider"] == "openai", ok
    assert ok["model"] == "gpt-4o-mini", ok
    assert ok["trace_id"] == "braintrust-e2e-trace-1", ok
    assert ok["sdk"] == "metergraphrelay", ok
    assert ok["latency_ms"] == LATENCY_MS, ok
    assert ok["status"] == "success", ok
    assert ok["error"] is False, ok

    # The contract this file exists for: an inclusive prompt_tokens total, with
    # cache reads and writes recorded as subsets of it rather than added on top.
    assert ok["input_tokens"] == PROMPT_TOKENS, ok
    assert ok["output_tokens"] == COMPLETION_TOKENS, ok
    assert ok["cache_read_tokens"] == CACHE_READ_TOKENS, ok
    assert ok["cache_write_tokens"] == CACHE_WRITE_TOKENS, ok
    assert ok["reasoning_tokens"] == REASONING_TOKENS, ok

    failed = items[ERROR_SPAN_ID]
    assert failed["status"] == "error", failed
    assert failed["error"] is True, failed
    assert failed["error_type"] == ERROR_MESSAGE, failed

    print("relay -> OSS Braintrust pull E2E passed")


def _check_live_braintrust() -> None:
    """Optional: one real /btql query, to confirm the query itself is accepted.

    Skipped unless BRAINTRUST_E2E_PROJECT is set. This is the only part that can
    catch a query the docs describe but the live API rejects -- the preview
    truncation setting, the cursor-compatible sort, the span-type filter.
    """
    project = os.environ.get("BRAINTRUST_E2E_PROJECT")
    if not project:
        print("BRAINTRUST_E2E_PROJECT unset - skipping the live Braintrust query")
        return
    api_key = os.environ.get("BRAINTRUST_API_KEY")
    if not api_key:
        raise SystemExit(
            "BRAINTRUST_E2E_PROJECT is set but BRAINTRUST_API_KEY is not."
        )
    base_url = os.environ.get("BRAINTRUST_BASE_URL", braintrust.DEFAULT_BRAINTRUST_URL)

    with tempfile.TemporaryDirectory() as temporary_directory:
        output = Path(temporary_directory) / "live.jsonl"
        imported, skipped = pull_braintrust(
            base_url=base_url,
            api_key=api_key,
            projects=[project],
            count=5,
            since=os.environ.get("BRAINTRUST_E2E_SINCE"),
            until=datetime.now(timezone.utc).isoformat(),
            route=None,
            output_path=str(output),
        )
        rows = [json.loads(line) for line in output.read_text().splitlines()]

    print(f"live Braintrust query returned {imported} span(s), skipped {skipped}")
    if not rows:
        print(
            "  no LLM spans in the window - the query was accepted but proved "
            "nothing about the mapping; widen it with BRAINTRUST_E2E_SINCE"
        )
        return
    sample = rows[0]
    # Content must arrive untruncated; a clipped preview would be the failure
    # SETTINGS preview_length = -1 exists to prevent.
    assert sample["content_opted_in"] is True, sample
    assert not str(sample.get("response_text") or "").endswith("..."), sample
    print(f"  sample: model={sample['model']!r} provider={sample['provider']!r}")


def main() -> None:
    base_url = os.environ.get("MG_URL", "http://127.0.0.1:8787")
    token = os.environ.get("MG_TOKEN", "ci-token")
    _check_pull_and_push(base_url, token)
    _check_live_braintrust()


if __name__ == "__main__":
    main()
