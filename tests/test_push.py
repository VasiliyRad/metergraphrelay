import json
import urllib.error
from unittest.mock import MagicMock, patch

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


def test_push_file_batches_versioned_import_rows(tmp_path):
    file_path = tmp_path / "traces.jsonl"
    rows = [
        {
            "import_source": "openai",
            "import_source_scope": "project-a",
            "import_event_id": f"event-{index}",
            "model": "gpt-test",
        }
        for index in range(501)
    ]
    file_path.write_text("".join(json.dumps(row) + "\n" for row in rows))

    with patch("metergraphrelay.push.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response(202)
        succeeded, failed = push_file(str(file_path), token="tok-123")

    assert (succeeded, failed) == (501, 0)
    assert mock_urlopen.call_count == 2
    first = json.loads(mock_urlopen.call_args_list[0].args[0].data)
    second = json.loads(mock_urlopen.call_args_list[1].args[0].data)
    assert len(first["rows"]) == 500
    assert len(second["rows"]) == 1
    assert first["meta"]["log_import"]["contract_version"] == 1
    assert first["meta"]["log_import"]["final"] is False
    assert second["meta"]["log_import"]["final"] is True
    assert (
        first["meta"]["log_import"]["run_id"] == second["meta"]["log_import"]["run_id"]
    )


def test_push_file_reuses_import_run_id_for_the_same_file(tmp_path):
    file_path = tmp_path / "traces.jsonl"
    row = {
        "import_source": "langfuse",
        "import_source_scope": "https://cloud.langfuse.com:project-a",
        "import_event_id": "generation-1",
        "model": "gpt-test",
    }
    file_path.write_text(json.dumps(row) + "\n")

    with patch("metergraphrelay.push.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response(202)
        assert push_file(str(file_path), token="tok-123") == (1, 0)
        assert push_file(str(file_path), token="tok-123") == (1, 0)

    first = json.loads(mock_urlopen.call_args_list[0].args[0].data)
    second = json.loads(mock_urlopen.call_args_list[1].args[0].data)
    assert (
        first["meta"]["log_import"]["run_id"] == second["meta"]["log_import"]["run_id"]
    )


def test_push_file_scopes_import_run_id_to_the_api_token(tmp_path):
    file_path = tmp_path / "traces.jsonl"
    row = {
        "import_source": "openai",
        "import_source_scope": "project-a",
        "import_event_id": "completion-1",
    }
    file_path.write_text(json.dumps(row) + "\n")

    with patch("metergraphrelay.push.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_response(202)
        push_file(str(file_path), token="tenant-token-a")
        push_file(str(file_path), token="tenant-token-b")

    first = json.loads(mock_urlopen.call_args_list[0].args[0].data)
    second = json.loads(mock_urlopen.call_args_list[1].args[0].data)
    assert (
        first["meta"]["log_import"]["run_id"] != second["meta"]["log_import"]["run_id"]
    )
