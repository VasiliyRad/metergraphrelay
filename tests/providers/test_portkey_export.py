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
SIGNED = "https://storage.example/signed?token=abc"


def _resp(status, body: bytes):
    """A response whose full body is returned by an unbounded read()."""
    r = MagicMock()
    r.status = status
    r.read.return_value = body
    r.__enter__.return_value = r
    r.__exit__.return_value = False
    return r


def _stream_resp(status, chunks):
    """A response that yields the given chunks through bounded read(size) calls."""
    r = MagicMock()
    r.status = status
    seq = list(chunks) + [b""]
    r.read.side_effect = lambda size=-1: seq.pop(0)
    r.__enter__.return_value = r
    r.__exit__.return_value = False
    return r


def _signed_ok(body_chunks):
    """urlopen side_effect: resolve the signed URL, then stream the export body."""
    return [
        _resp(200, json.dumps({"signed_url": SIGNED}).encode()),
        _stream_resp(200, body_chunks),
    ]


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
        export = PortkeyExportClient("pk-secret", base_url=BASE).create_export(
            window_start=W_MIN, window_end=W_MAX
        )
    assert "workspace_id" not in json.loads(mock.call_args.args[0].data)
    assert export.total == 0  # zero is a valid, nonnegative total


@pytest.mark.parametrize(
    "payload",
    [
        {"total": 1},                    # id missing
        {"id": None, "total": 1},        # id null
        {"id": "", "total": 1},          # id empty
        {"id": 123, "total": 1},         # id non-string
        {"id": True, "total": 1},        # id bool (non-string)
        {"id": "exp-1"},                 # total missing
        {"id": "exp-1", "total": None},  # total null
        {"id": "exp-1", "total": "5"},   # total string
        {"id": "exp-1", "total": -1},    # total negative
        {"id": "exp-1", "total": True},  # total bool
        {"id": "exp-1", "total": 1.5},   # total float
    ],
)
def test_create_export_rejects_invalid_id_or_total(payload):
    with patch("metergraphrelay.providers.portkey_export.urllib.request.urlopen") as mock:
        mock.return_value = _resp(200, json.dumps(payload).encode())
        with pytest.raises(PortkeyExportError):
            _client().create_export(window_start=W_MIN, window_end=W_MAX)


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


def test_get_export_defaults_id_to_argument_when_absent():
    body = json.dumps({"status": "success"}).encode()
    with patch("metergraphrelay.providers.portkey_export.urllib.request.urlopen") as mock:
        mock.return_value = _resp(200, body)
        export = _client().get_export("exp-1")
    assert export.export_id == "exp-1"
    assert export.is_success


@pytest.mark.parametrize(
    "payload",
    [
        {"id": "exp-1"},                        # status missing
        {"id": "exp-1", "status": None},        # status null
        {"id": "exp-1", "status": ""},          # status empty
        {"id": "exp-1", "status": 123},         # status non-string
        {"id": "exp-1", "status": "unknown"},   # status not in enum
        {"id": "exp-1", "status": "SUCCESS"},   # wrong case
        {"id": "", "status": "success"},        # id present but empty
        {"id": 123, "status": "success"},       # id present but non-string
    ],
)
def test_get_export_rejects_invalid_status_or_id(payload):
    with patch("metergraphrelay.providers.portkey_export.urllib.request.urlopen") as mock:
        mock.return_value = _resp(200, json.dumps(payload).encode())
        with pytest.raises(PortkeyExportError):
            _client().get_export("exp-1")


@pytest.mark.parametrize(
    "status, terminal, success",
    [("success", True, True), ("failed", True, False), ("stopped", True, False),
     ("in_progress", False, False), ("draft", False, False)],
)
def test_export_terminal_and_success_flags(status, terminal, success):
    export = PortkeyExport(export_id="exp-1", total=None, status=status)
    assert export.is_terminal is terminal
    assert export.is_success is success


def test_download_to_resolves_signed_url_then_streams_without_portkey_header(tmp_path):
    payload = b'{"id":"r1"}\n{"id":"r2"}\n'
    dest = tmp_path / "raw.jsonl"
    with patch("metergraphrelay.providers.portkey_export.urllib.request.urlopen") as mock:
        mock.side_effect = _signed_ok([payload])
        written = _client().download_to("exp-1", str(dest))

    first, second = mock.call_args_list[0].args[0], mock.call_args_list[1].args[0]
    assert first.full_url == f"{BASE}/logs/exports/exp-1/download"
    assert first.get_header("X-portkey-api-key") == "pk-secret"
    assert second.full_url == SIGNED
    assert second.get_header("X-portkey-api-key") is None  # pre-signed: no credential leaked
    assert written == 2
    assert dest.read_bytes() == payload


def test_download_to_streams_bounded_chunks_and_counts_split_lines(tmp_path):
    dest = tmp_path / "raw.jsonl"
    # A record split across a chunk boundary, interleaved with blank lines that
    # must NOT be counted, and a final record with no trailing newline.
    chunks = [b'{"id":', b'"r1"}\n\n', b'{"id":"r2"}\n', b'   \n', b'{"id":"r3"}']
    stream = _stream_resp(200, chunks)
    with patch("metergraphrelay.providers.portkey_export.urllib.request.urlopen") as mock:
        mock.side_effect = [
            _resp(200, json.dumps({"signed_url": SIGNED}).encode()),
            stream,
        ]
        written = _client().download_to("exp-1", str(dest))

    # Every read of the signed body must be bounded (a positive size argument),
    # never an unbounded read().
    assert stream.read.call_args_list  # it was actually read
    for call in stream.read.call_args_list:
        assert call.args, "signed body was read without a bounded size"
        assert isinstance(call.args[0], int) and call.args[0] > 0
    assert written == 3  # r1 (split across chunks), r2, r3 (no trailing newline); blanks ignored
    assert dest.read_bytes() == b"".join(chunks)


def test_download_to_invokes_progress_callback_at_bounded_cadence(tmp_path):
    dest = tmp_path / "raw.jsonl"
    chunks = [b'{"id":"r1"}\n', b'{"id":"r2"}\n', b'{"id":"r3"}\n']
    calls = []
    with patch("metergraphrelay.providers.portkey_export.urllib.request.urlopen") as mock:
        mock.side_effect = [
            _resp(200, json.dumps({"signed_url": SIGNED}).encode()),
            _stream_resp(200, chunks),
        ]
        _client().download_to("exp-1", str(dest), on_progress=lambda: calls.append(1))
    # One bounded-cadence callback per streamed chunk, so Task 5 can renew mid-download.
    assert len(calls) == len(chunks)


def test_download_to_without_callback_still_downloads(tmp_path):
    dest = tmp_path / "raw.jsonl"
    with patch("metergraphrelay.providers.portkey_export.urllib.request.urlopen") as mock:
        mock.side_effect = _signed_ok([b'{"id":"r1"}\n'])
        assert _client().download_to("exp-1", str(dest)) == 1
    assert dest.read_bytes() == b'{"id":"r1"}\n'


def test_download_to_leaves_no_partial_file_on_midstream_failure(tmp_path):
    dest = tmp_path / "raw.jsonl"

    def failing_stream():
        r = MagicMock()
        r.status = 200
        r.read.side_effect = [b'{"id":"r1"}\n', urllib.error.URLError("boom")]
        r.__enter__.return_value = r
        r.__exit__.return_value = False
        return r

    with patch("metergraphrelay.providers.portkey_export.urllib.request.urlopen") as mock:
        mock.side_effect = [
            _resp(200, json.dumps({"signed_url": SIGNED}).encode()),
            failing_stream(),
        ]
        with pytest.raises(PortkeyExportError, match="boom"):
            _client().download_to("exp-1", str(dest))

    assert not dest.exists()  # never atomically replaced with partial content
    assert not (tmp_path / "raw.jsonl.part").exists()  # temp sibling cleaned up


@pytest.mark.parametrize(
    "bad_url",
    ["ftp://host/x", "file:///etc/passwd", "javascript:alert(1)", "http:///nohost", "notaurl", ""],
)
def test_download_to_rejects_non_http_signed_url(bad_url):
    with patch("metergraphrelay.providers.portkey_export.urllib.request.urlopen") as mock:
        mock.return_value = _resp(200, json.dumps({"signed_url": bad_url}).encode())
        with pytest.raises(PortkeyExportError, match="signed_url"):
            _client().download_to("exp-1", "/tmp/whatever.jsonl")
    # Only the resolution call happened; a malformed/non-http URL is never fetched,
    # so no credential could leak to it.
    assert mock.call_count == 1


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


def test_api_endpoint_unexpected_2xx_status_raises():
    # urlopen does not raise on 2xx; the client must reject any status other than 200.
    with patch("metergraphrelay.providers.portkey_export.urllib.request.urlopen") as mock:
        mock.return_value = _resp(202, json.dumps({"id": "exp-1", "total": 1}).encode())
        with pytest.raises(PortkeyExportError, match="202"):
            _client().create_export(window_start=W_MIN, window_end=W_MAX)


def test_signed_fetch_unexpected_status_raises_and_writes_nothing(tmp_path):
    dest = tmp_path / "raw.jsonl"
    with patch("metergraphrelay.providers.portkey_export.urllib.request.urlopen") as mock:
        mock.side_effect = [
            _resp(200, json.dumps({"signed_url": SIGNED}).encode()),
            _stream_resp(206, [b'{"id":"r1"}\n']),
        ]
        with pytest.raises(PortkeyExportError, match="206"):
            _client().download_to("exp-1", str(dest))
    assert not dest.exists()
    assert not (tmp_path / "raw.jsonl.part").exists()


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
