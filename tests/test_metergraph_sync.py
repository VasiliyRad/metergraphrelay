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
