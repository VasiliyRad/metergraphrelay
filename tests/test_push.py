import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from metergraphrelay.push import DEFAULT_INGEST_URL, push_file


def _mock_response(status, *, accepted=None):
    """A push response. ``accepted`` defaults to matching whatever the batch
    turns out to contain (the common "everything landed" case); pass an int
    to simulate the server ignoring some rows within an otherwise-202 batch.
    """
    response = MagicMock()
    response.status = status
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    if accepted is not None:
        response.read.return_value = json.dumps(
            {"accepted": accepted, "ignored": 0, "batch": "b1"}
        ).encode()
    else:
        # No explicit accepted count given: return an unparseable body, which
        # push_file falls back on treating as "the whole batch landed".
        response.read.return_value = b"not json"
    return response


def test_push_file_batches_rows_into_one_request_and_counts_success(tmp_path):
    file_path = tmp_path / "traces.jsonl"
    file_path.write_text('{"a": 1}\n{"a": 2}\n')

    with patch("metergraphrelay.push.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response(202, accepted=2)
        succeeded, failed = push_file(str(file_path), token="tok-123")

    assert succeeded == 2
    assert failed == 0
    assert mock_urlopen.call_count == 1
    request = mock_urlopen.call_args_list[0].args[0]
    assert request.full_url == f"{DEFAULT_INGEST_URL}/v1/ingest"
    assert request.get_header("Authorization") == "Bearer tok-123"
    assert request.get_header("Content-type") == "application/json"
    body = json.loads(request.data)
    assert body == {"schema_version": 1, "rows": [{"a": 1}, {"a": 2}], "meta": {}}


def test_push_file_uses_custom_base_url(tmp_path):
    file_path = tmp_path / "traces.jsonl"
    file_path.write_text('{"a": 1}\n')

    with patch("metergraphrelay.push.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response(202, accepted=1)
        push_file(str(file_path), token="tok-123", base_url="http://localhost:8080")

    request = mock_urlopen.call_args_list[0].args[0]
    assert request.full_url == "http://localhost:8080/v1/ingest"


def test_push_file_skips_blank_lines(tmp_path):
    file_path = tmp_path / "traces.jsonl"
    file_path.write_text('{"a": 1}\n\n{"a": 2}\n')

    with patch("metergraphrelay.push.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response(202, accepted=2)
        succeeded, failed = push_file(str(file_path), token="tok-123")

    assert succeeded == 2
    assert failed == 0
    assert mock_urlopen.call_count == 1  # both rows land in the one batch


def test_push_file_flushes_a_new_batch_once_the_row_count_bound_is_hit(tmp_path):
    file_path = tmp_path / "traces.jsonl"
    file_path.write_text('{"a": 1}\n{"a": 2}\n{"a": 3}\n')

    with patch("metergraphrelay.push.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = [_mock_response(202, accepted=2), _mock_response(202, accepted=1)]
        succeeded, failed = push_file(str(file_path), token="tok-123", max_rows_per_batch=2)

    assert succeeded == 3
    assert failed == 0
    assert mock_urlopen.call_count == 2
    first_body = json.loads(mock_urlopen.call_args_list[0].args[0].data)
    second_body = json.loads(mock_urlopen.call_args_list[1].args[0].data)
    assert first_body["rows"] == [{"a": 1}, {"a": 2}]
    assert second_body["rows"] == [{"a": 3}]


def test_push_file_flushes_a_new_batch_once_the_byte_bound_is_hit(tmp_path):
    file_path = tmp_path / "traces.jsonl"
    file_path.write_text('{"a": 1}\n{"a": 2}\n')
    # A bound that fits exactly one encoded row but not two forces a flush
    # between them.
    tiny_bound = len(json.dumps({"a": 1}).encode())

    with patch("metergraphrelay.push.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = [_mock_response(202, accepted=1), _mock_response(202, accepted=1)]
        succeeded, failed = push_file(str(file_path), token="tok-123", max_batch_bytes=tiny_bound)

    assert succeeded == 2
    assert failed == 0
    assert mock_urlopen.call_count == 2


def test_push_file_counts_ignored_rows_within_a_202_batch_as_failed(tmp_path):
    """A 202 status means the batch request was accepted, not that every row
    in it was durably stored -- e.g. a content-capture policy can silently
    drop a row. Those must count as failed, not succeeded."""
    file_path = tmp_path / "traces.jsonl"
    file_path.write_text('{"a": 1}\n{"a": 2}\n')

    with patch("metergraphrelay.push.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response(202, accepted=1)  # 1 of 2 ignored
        succeeded, failed = push_file(str(file_path), token="tok-123")

    assert succeeded == 1
    assert failed == 1


def test_push_file_counts_http_error_and_continues(tmp_path):
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
        # One row per batch, so the first row's failure doesn't take the second down with it.
        mock_urlopen.side_effect = [http_error, _mock_response(202, accepted=1)]
        succeeded, failed = push_file(str(file_path), token="bad-token", max_rows_per_batch=1)

    assert succeeded == 1
    assert failed == 1


def test_push_file_http_error_fails_the_whole_batch(tmp_path, capsys):
    file_path = tmp_path / "traces.jsonl"
    file_path.write_text('{"a": 1}\n{"a": 2}\n')
    http_error = urllib.error.HTTPError(
        url=f"{DEFAULT_INGEST_URL}/v1/ingest", code=401, msg="Unauthorized", hdrs=None, fp=None,
    )

    with patch("metergraphrelay.push.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = http_error
        succeeded, failed = push_file(str(file_path), token="bad-token")

    assert succeeded == 0
    assert failed == 2  # both rows were in the one failed batch
    assert "401" in capsys.readouterr().err


def test_push_file_counts_malformed_json_line_and_continues(tmp_path, capsys):
    file_path = tmp_path / "traces.jsonl"
    file_path.write_text('{"a": 1}\nnot json at all\n{"a": 2}\n')

    with patch("metergraphrelay.push.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response(202, accepted=2)
        succeeded, failed = push_file(str(file_path), token="tok-123")

    assert succeeded == 2
    assert failed == 1
    assert mock_urlopen.call_count == 1  # the two valid rows still batch together
    captured = capsys.readouterr()
    assert "line 2" in captured.err


def test_push_file_counts_url_error_and_continues(tmp_path):
    file_path = tmp_path / "traces.jsonl"
    file_path.write_text('{"a": 1}\n')
    url_error = urllib.error.URLError("connection refused")

    with patch("metergraphrelay.push.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = url_error
        succeeded, failed = push_file(str(file_path), token="tok-123")

    assert succeeded == 0
    assert failed == 1


class _StopProgress(Exception):
    """A sentinel exception raised from on_progress to prove it is not swallowed."""


def _raise_stop():
    raise _StopProgress()


def test_push_file_invokes_on_progress_once_per_processed_row(tmp_path):
    file_path = tmp_path / "traces.jsonl"
    file_path.write_text('{"a": 1}\n{"a": 2}\n{"a": 3}\n')
    ticks = []

    with patch("metergraphrelay.push.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response(202, accepted=3)
        push_file(str(file_path), token="tok-123", on_progress=lambda: ticks.append(1))

    assert len(ticks) == 3  # one progress tick per row uploaded, even though batched into 1 request


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
        mock_urlopen.return_value = _mock_response(202, accepted=2)
        result = push_file(str(file_path), token="tok-123")

    assert result == (2, 0)  # default None on_progress: behavior identical to before


def test_push_file_propagates_on_progress_exception(tmp_path):
    file_path = tmp_path / "traces.jsonl"
    file_path.write_text('{"a": 1}\n')

    with patch("metergraphrelay.push.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response(202, accepted=1)
        with pytest.raises(_StopProgress):
            push_file(str(file_path), token="tok-123", on_progress=_raise_stop)
