"""Latest relay -> latest OSS server timestamp contract smoke test."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import urllib.parse
import urllib.request
import uuid

from metergraphrelay.providers.portkey import convert_portkey_export
from metergraphrelay.push import push_file


HISTORICAL_TIMESTAMP = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
ROUTE_PREFIX = "relay.historical-timestamp-e2e"


def main() -> None:
    base_url = os.environ.get("MG_URL", "http://127.0.0.1:8787")
    token = os.environ.get("MG_TOKEN", "ci-token")
    route = f"{ROUTE_PREFIX}.{uuid.uuid4().hex}"
    row = {
        "id": "relay-historical-timestamp-1",
        "trace_id": "relay-historical-trace-1",
        # Millisecond epochs previously reached OSS unchanged and were replaced
        # with ingestion time because the server accepts epoch seconds only.
        "created_at": int(HISTORICAL_TIMESTAMP.timestamp() * 1000),
        "ai_org": "openai",
        "ai_model": "gpt-4o-mini",
        "cost": 0.01,
        "req_units": 10,
        "res_units": 5,
        "response_time": 100,
        "response_status_code": 200,
        "request": {"model": "gpt-4o-mini", "input": "timestamp smoke"},
        "response": {"object": "response", "output": []},
        "metadata": {"workflow_name": route},
    }

    with tempfile.TemporaryDirectory() as temporary_directory:
        directory = Path(temporary_directory)
        source = directory / "portkey.jsonl"
        converted = directory / "metergraph.jsonl"
        source.write_text(json.dumps(row) + "\n")

        assert convert_portkey_export(str(source), str(converted)) == (1, 0)
        normalized = json.loads(converted.read_text())
        assert normalized["ts"] == "2026-08-10T12:00:00Z"
        assert push_file(str(converted), token=token, base_url=base_url) == (1, 0)

    query = urllib.parse.urlencode({"route": route})
    request = urllib.request.Request(
        f"{base_url}/v1/calls?{query}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.load(response)
    matches = [item for item in payload["items"] if item["route"] == route]
    assert len(matches) == 1, matches
    observed = datetime.fromisoformat(matches[0]["ts"]).astimezone(timezone.utc)
    assert observed == HISTORICAL_TIMESTAMP, matches[0]
    print("relay -> OSS historical timestamp E2E passed")


if __name__ == "__main__":
    main()
