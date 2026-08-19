import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from metergraphrelay.push import DEFAULT_INGEST_URL, push_file


def _mock_response(status):
    response = MagicMock()
    response.status = status
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    return response


def test_push_file_sends_each_row_and_counts_success(tmp_path):
    file_path = tmp_path / "traces.jsonl"
    file_path.write_text('{"a": 1}\n{"a": 2}\n')

    with patch("metergraphrelay.push.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response(202)
        succeeded, failed = push_file(str(file_path), token="tok-123")

    assert succeeded == 2
    assert failed == 0
    assert mock_urlopen.call_count == 2
    first_request = mock_urlopen.call_args_list[0].args[0]
    assert first_request.full_url == f"{DEFAULT_INGEST_URL}/v1/ingest"
    assert first_request.get_header("Authorization") == "Bearer tok-123"
    assert first_request.get_header("Content-type") == "application/json"
    body = json.loads(first_request.data)
    assert body == {"schema_version": 1, "rows": [{"a": 1}], "meta": {}}


def test_push_file_uses_custom_base_url(tmp_path):
    file_path = tmp_path / "traces.jsonl"
    file_path.write_text('{"a": 1}\n')

    with patch("metergraphrelay.push.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response(202)
        push_file(str(file_path), token="tok-123", base_url="http://localhost:8080")

    request = mock_urlopen.call_args_list[0].args[0]
    assert request.full_url == "http://localhost:8080/v1/ingest"


def test_push_file_skips_blank_lines(tmp_path):
    file_path = tmp_path / "traces.jsonl"
    file_path.write_text('{"a": 1}\n\n{"a": 2}\n')

    with patch("metergraphrelay.push.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response(202)
        succeeded, failed = push_file(str(file_path), token="tok-123")

    assert succeeded == 2
    assert failed == 0
    assert mock_urlopen.call_count == 2


def test_push_file_counts_http_error_and_continues(tmp_path, capsys):
    file_path = tmp_path / "traces.jsonl"
    file_path.write_text('{"a": 1}\n{"a": 2}\n')
    http_error = urllib.error.HTTPError(
        url=f"{DEFAULT_INGEST_URL}/v1/ingest",
        code=401,
        msg="Unauthorized",
        hdrs=None,
        fp=None,
    )

    with patch("metergraphrelay.push.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = [http_error, _mock_response(202)]
        succeeded, failed = push_file(str(file_path), token="bad-token")

    assert succeeded == 1
    assert failed == 1
    captured = capsys.readouterr()
    assert "401" in captured.err


def test_push_file_counts_malformed_json_line_and_continues(tmp_path, capsys):
    file_path = tmp_path / "traces.jsonl"
    file_path.write_text('{"a": 1}\nnot json at all\n{"a": 2}\n')

    with patch("metergraphrelay.push.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response(202)
        succeeded, failed = push_file(str(file_path), token="tok-123")

    assert succeeded == 2
    assert failed == 1
    assert mock_urlopen.call_count == 2
    captured = capsys.readouterr()
    assert "line 2" in captured.err


def test_push_file_counts_url_error_and_continues(tmp_path, capsys):
    file_path = tmp_path / "traces.jsonl"
    file_path.write_text('{"a": 1}\n')
    url_error = urllib.error.URLError("connection refused")

    with patch("metergraphrelay.push.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = url_error
        succeeded, failed = push_file(str(file_path), token="tok-123")

    assert succeeded == 0
    assert failed == 1
    captured = capsys.readouterr()
    assert "connection refused" in captured.err


class _StopProgress(Exception):
    """A sentinel exception raised from on_progress to prove it is not swallowed."""


def _raise_stop():
    raise _StopProgress()


def test_push_file_invokes_on_progress_once_per_processed_row(tmp_path):
    file_path = tmp_path / "traces.jsonl"
    file_path.write_text('{"a": 1}\n{"a": 2}\n{"a": 3}\n')
    ticks = []

    with patch("metergraphrelay.push.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response(202)
        push_file(str(file_path), token="tok-123", on_progress=lambda: ticks.append(1))

    assert len(ticks) == 3  # one progress tick per row uploaded


def test_push_file_on_progress_fires_for_failed_and_malformed_rows_too(tmp_path):
    file_path = tmp_path / "traces.jsonl"
    # A malformed line, a blank line, and a row whose request fails.
    file_path.write_text('not-json\n\n{"a": 1}\n')
    ticks = []

    with patch("metergraphrelay.push.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response(500)  # non-202 -> counted failed
        push_file(str(file_path), token="tok-123", on_progress=lambda: ticks.append(1))

    assert len(ticks) == 2  # both non-blank lines processed; blank line skipped


def test_push_file_without_on_progress_is_unchanged(tmp_path):
    file_path = tmp_path / "traces.jsonl"
    file_path.write_text('{"a": 1}\n{"a": 2}\n')

    with patch("metergraphrelay.push.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response(202)
        result = push_file(str(file_path), token="tok-123")

    assert result == (2, 0)  # default None on_progress: behavior identical to before


def test_push_file_propagates_on_progress_exception(tmp_path):
    file_path = tmp_path / "traces.jsonl"
    file_path.write_text('{"a": 1}\n')

    with patch("metergraphrelay.push.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response(202)
        with pytest.raises(_StopProgress):
            push_file(str(file_path), token="tok-123", on_progress=_raise_stop)
