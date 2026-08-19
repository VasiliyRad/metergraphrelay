import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from metergraphrelay.providers.portkey_export import (
    STATUS_SUCCESS,
    PortkeyExport,
    PortkeyExportClient,
    PortkeyExportError,
)

BASE = "https://api.portkey.example/v1"
W_MIN = "2026-08-19T00:00:00+00:00"
W_MAX = "2026-08-19T01:00:00+00:00"


def _resp(status, body: bytes):
    r = MagicMock()
    r.status = status
    r.read.return_value = body
    r.__enter__.return_value = r
    r.__exit__.return_value = False
    return r


def _client():
    return PortkeyExportClient("pk-secret", workspace="ws-acme", base_url=BASE)


def test_create_export_sends_filters_requested_data_and_api_key_header():
    body = json.dumps({"id": "exp-1", "total": 42, "object": "export"}).encode()
    with patch("metergraphrelay.providers.portkey_export.urllib.request.urlopen") as mock:
        mock.return_value = _resp(200, body)
        export = _client().create_export(window_start=W_MIN, window_end=W_MAX)

    request = mock.call_args.args[0]
    assert request.full_url == f"{BASE}/logs/exports"
    assert request.method == "POST"
    assert request.get_header("X-portkey-api-key") == "pk-secret"  # urllib title-cases header keys
    sent = json.loads(request.data)
    assert sent["workspace_id"] == "ws-acme"
    assert sent["filters"]["time_of_generation_min"] == W_MIN
    assert sent["filters"]["time_of_generation_max"] == W_MAX
    assert sent["filters"]["page_size"] == 50000
    assert sent["filters"]["current_page"] == 1
    # requested_data pulls exactly the fields the normalizer consumes.
    assert "created_at" in sent["requested_data"]
    assert "response_status_code" in sent["requested_data"]
    assert "metadata" in sent["requested_data"]
    # The draft's total is known immediately, before start.
    assert export.export_id == "exp-1"
    assert export.total == 42
    assert export.status == "draft"
    assert not export.is_terminal


def test_create_export_omits_workspace_id_when_not_configured():
    body = json.dumps({"id": "exp-1", "total": 0, "object": "export"}).encode()
    with patch("metergraphrelay.providers.portkey_export.urllib.request.urlopen") as mock:
        mock.return_value = _resp(200, body)
        PortkeyExportClient("pk-secret", base_url=BASE).create_export(
            window_start=W_MIN, window_end=W_MAX
        )
    assert "workspace_id" not in json.loads(mock.call_args.args[0].data)


def test_start_export_posts_to_start_path():
    with patch("metergraphrelay.providers.portkey_export.urllib.request.urlopen") as mock:
        mock.return_value = _resp(200, json.dumps({"message": "ok", "object": "export"}).encode())
        _client().start_export("exp-1")
    request = mock.call_args.args[0]
    assert request.full_url == f"{BASE}/logs/exports/exp-1/start"
    assert request.method == "POST"


def test_get_export_reads_status_enum():
    body = json.dumps({"id": "exp-1", "status": "in_progress", "object": "export"}).encode()
    with patch("metergraphrelay.providers.portkey_export.urllib.request.urlopen") as mock:
        mock.return_value = _resp(200, body)
        export = _client().get_export("exp-1")
    request = mock.call_args.args[0]
    assert request.full_url == f"{BASE}/logs/exports/exp-1"
    assert request.method == "GET"
    assert export.status == "in_progress"
    assert not export.is_terminal


@pytest.mark.parametrize(
    "status, terminal, success",
    [("success", True, True), ("failed", True, False), ("stopped", True, False),
     ("in_progress", False, False), ("draft", False, False)],
)
def test_export_terminal_and_success_flags(status, terminal, success):
    export = PortkeyExport(export_id="exp-1", total=None, status=status)
    assert export.is_terminal is terminal
    assert export.is_success is success


def test_download_to_resolves_signed_url_then_fetches_it_without_portkey_header(tmp_path):
    signed = "https://storage.example/signed?token=abc"
    payload = b'{"id":"r1"}\n{"id":"r2"}\n'
    dest = tmp_path / "raw.jsonl"
    with patch("metergraphrelay.providers.portkey_export.urllib.request.urlopen") as mock:
        mock.side_effect = [
            _resp(200, json.dumps({"signed_url": signed}).encode()),  # GET .../download
            _resp(200, payload),                                       # GET signed URL
        ]
        written = _client().download_to("exp-1", str(dest))

    first, second = mock.call_args_list[0].args[0], mock.call_args_list[1].args[0]
    assert first.full_url == f"{BASE}/logs/exports/exp-1/download"
    assert first.get_header("X-portkey-api-key") == "pk-secret"
    assert second.full_url == signed
    assert second.get_header("X-portkey-api-key") is None  # pre-signed: no credential leaked
    assert written == 2
    assert dest.read_bytes() == payload


def test_download_to_raises_when_signed_url_missing():
    with patch("metergraphrelay.providers.portkey_export.urllib.request.urlopen") as mock:
        mock.return_value = _resp(200, json.dumps({}).encode())
        with pytest.raises(PortkeyExportError, match="signed_url"):
            _client().download_to("exp-1", "/tmp/whatever.jsonl")


def test_cancel_export_posts_to_cancel_path():
    with patch("metergraphrelay.providers.portkey_export.urllib.request.urlopen") as mock:
        mock.return_value = _resp(200, json.dumps({"message": "cancelled", "object": "export"}).encode())
        _client().cancel_export("exp-1")
    request = mock.call_args.args[0]
    assert request.full_url == f"{BASE}/logs/exports/exp-1/cancel"
    assert request.method == "POST"


def test_http_error_raises_portkey_export_error():
    with patch("metergraphrelay.providers.portkey_export.urllib.request.urlopen") as mock:
        mock.side_effect = urllib.error.HTTPError(
            url=f"{BASE}/x", code=401, msg="Unauthorized", hdrs=None, fp=None
        )
        with pytest.raises(PortkeyExportError, match="401"):
            _client().get_export("exp-1")


def test_network_error_raises_portkey_export_error():
    with patch("metergraphrelay.providers.portkey_export.urllib.request.urlopen") as mock:
        mock.side_effect = urllib.error.URLError("connection refused")
        with pytest.raises(PortkeyExportError, match="connection refused"):
            _client().get_export("exp-1")
