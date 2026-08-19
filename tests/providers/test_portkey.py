import json
import os
from unittest.mock import patch

import pytest

from metergraphrelay import __version__
from metergraphrelay.cli import build_parser, main
from metergraphrelay.providers.portkey import (
    ImportContext,
    PortkeyConversionError,
    convert_portkey_export,
    normalize_portkey_row,
)


def _responses_row(**overrides):
    row = {
        "id": "pk-req-1",
        "trace_id": "trace-1",
        "created_at": "2026-08-10T12:00:00Z",
        "ai_org": "openai",
        "ai_model": "gpt-5",
        "cost": 12.5,
        "req_units": 100,
        "res_units": 40,
        "response_time": 850,
        "response_status_code": 200,
        "request": {
            "model": "gpt-5",
            "input": "What's the latest on X?",
            "tools": [{"type": "web_search"}],
        },
        "response": {
            "object": "response",
            "output": [
                {
                    "type": "web_search_call",
                    "id": "ws-1",
                    "status": "completed",
                    "action": {"type": "search", "query": "latest X"},
                },
                {
                    "type": "message",
                    "id": "msg-1",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": "Here is the latest on X."}
                    ],
                },
            ],
        },
        "metadata": {
            "workflow_name": "news-digest",
            "activity_name": "summarize",
            "organization_id": "org-42",
        },
    }
    row.update(overrides)
    return row


def _chat_completion_row(**overrides):
    row = {
        "id": "pk-req-2",
        "trace_id": "trace-2",
        "created_at": "2026-08-10T12:05:00Z",
        "ai_org": "vertex-ai",
        "ai_model": "gemini-2.0-flash",
        "cost": 3.0,
        "req_units": 50,
        "res_units": 20,
        "response_time": 400,
        "response_status_code": 200,
        "request": {
            "model": "gemini-2.0-flash",
            "messages": [{"role": "user", "content": "search for cats"}],
            "tools": [{"type": "function", "function": {"name": "google_search"}}],
        },
        "response": {
            "object": "chat.completion",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "google_search",
                                    "arguments": '{"query": "cats"}',
                                },
                            }
                        ],
                    },
                    "index": 0,
                    "finish_reason": "tool_calls",
                }
            ],
        },
        "metadata": {"workflow_name": "search-bot", "activity_name": "search"},
    }
    row.update(overrides)
    return row


def _anthropic_row(**overrides):
    row = {
        "id": "pk-req-3",
        "trace_id": "trace-3",
        "created_at": "2026-08-10T12:10:00Z",
        "ai_org": "anthropic",
        "ai_model": "claude-sonnet-5",
        "cost": 8.0,
        "req_units": 200,
        "res_units": 60,
        "response_time": 1200,
        "response_status_code": 200,
        "request": {
            "model": "claude-sonnet-5",
            "messages": [{"role": "user", "content": "what's the weather"}],
            "tools": [
                {
                    "name": "get_weather",
                    "description": "Get current weather",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
        },
        "response": {
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me check that for you."},
                {
                    "type": "tool_use",
                    "id": "toolu-1",
                    "name": "get_weather",
                    "input": {"location": "SF"},
                },
            ],
        },
        "metadata": {"workflow_name": "weather-bot", "activity_name": "lookup"},
    }
    row.update(overrides)
    return row


def test_normalize_portkey_row_maps_verified_fields():
    row = _responses_row()

    result = normalize_portkey_row(row)

    assert result["ts"] == "2026-08-10T12:00:00Z"
    assert result["provider"] == "openai"
    assert result["model"] == "gpt-5"
    assert result["input_tokens"] == 100
    assert result["output_tokens"] == 40
    assert result["latency_ms"] == 850
    assert result["status"] == "success"
    assert result["error"] is False
    assert result["error_type"] is None
    assert result["cost_usd"] == 0.125
    assert result["request_id"] == "pk-req-1"
    assert result["span_id"] == "pk-req-1"
    assert result["trace_id"] == "trace-1"
    assert result["route"] == "news-digest"
    assert result["tags"] == row["metadata"]
    assert result["sdk"] == "metergraphrelay"
    assert result["sdk_version"] == __version__
    assert result["content_opted_in"] is True


@pytest.mark.parametrize(
    ("created_at", "expected"),
    [
        ("2026-08-10T05:00:00-07:00", "2026-08-10T12:00:00Z"),
        ("2026-08-10T12:00:00.123456Z", "2026-08-10T12:00:00.123456Z"),
        (
            "Sat Aug 08 2026 07:30:00 GMT+0000 (Coordinated Universal Time)",
            "2026-08-08T07:30:00Z",
        ),
        (1786363200, "2026-08-10T12:00:00Z"),
        (1786363200000, "2026-08-10T12:00:00Z"),
        ("1786363200000", "2026-08-10T12:00:00Z"),
    ],
)
def test_normalize_portkey_row_canonicalizes_timestamp_to_rfc3339_utc(
    created_at, expected
):
    result = normalize_portkey_row(_responses_row(created_at=created_at))

    assert result["ts"] == expected


@pytest.mark.parametrize(
    "created_at",
    [None, "", "not-a-timestamp", "2026-08-10T12:00:00", True],
)
def test_normalize_portkey_row_rejects_invalid_timestamp(created_at):
    with pytest.raises(PortkeyConversionError, match="created_at"):
        normalize_portkey_row(_responses_row(created_at=created_at))


def test_normalize_portkey_row_rejects_missing_timestamp():
    row = _responses_row()
    del row["created_at"]

    with pytest.raises(PortkeyConversionError, match="created_at"):
        normalize_portkey_row(row)


def test_normalize_portkey_row_route_falls_back_when_workflow_name_missing():
    row = _responses_row(metadata={"activity_name": "summarize"})

    result = normalize_portkey_row(row)

    assert result["route"] == "portkey/backfill"
    assert result["tags"] == {"activity_name": "summarize"}


def test_normalize_portkey_row_error_status_code_sets_error_fields():
    row = _responses_row(
        response_status_code=429,
        response={"error": {"message": "rate limited"}, "provider": "openai"},
    )

    result = normalize_portkey_row(row)

    assert result["status"] == "error"
    assert result["error"] is True
    assert result["error_type"] == "rate limited"
    assert result["response_text"] == json.dumps(row["response"])
    assert result["tool_calls"] is None


@pytest.mark.parametrize(
    "error_value", [["timeout", "retry-later"], 500, True]
)
def test_normalize_portkey_row_error_type_serializes_non_string_non_dict_error(
    error_value,
):
    row = _responses_row(
        response_status_code=500,
        response={"error": error_value, "provider": "openai"},
    )

    result = normalize_portkey_row(row)

    assert result["error_type"] == json.dumps(error_value)


def test_normalize_portkey_row_error_type_stays_none_when_error_key_absent():
    row = _responses_row(
        response_status_code=500, response={"provider": "openai"}
    )

    result = normalize_portkey_row(row)

    assert result["error_type"] is None


@pytest.mark.parametrize("missing_field", ["id", "trace_id"])
def test_normalize_portkey_row_missing_required_field_raises_key_error(missing_field):
    row = _responses_row()
    del row[missing_field]

    with pytest.raises(KeyError):
        normalize_portkey_row(row)


def test_normalize_portkey_row_openai_responses_hosted_web_search():
    row = _responses_row()

    result = normalize_portkey_row(row)

    assert result["response_text"] == "Here is the latest on X."
    assert result["tool_calls"] == [
        {
            "type": "web_search_call",
            "id": "ws-1",
            "status": "completed",
            "action": {"type": "search", "query": "latest X"},
        }
    ]
    assert result["tool_names"] == ["web_search_call"]
    assert result["request_json"] == json.dumps(row["request"])
    assert json.loads(result["request_json"])["tools"] == [{"type": "web_search"}]


def test_normalize_portkey_row_vertex_function_style_google_search():
    row = _chat_completion_row()

    result = normalize_portkey_row(row)

    assert result["response_text"] is None
    assert result["tool_calls"] == [
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "google_search", "arguments": '{"query": "cats"}'},
        }
    ]
    assert result["tool_names"] == ["google_search"]


def test_normalize_portkey_row_anthropic_native_tools():
    row = _anthropic_row()

    result = normalize_portkey_row(row)

    assert result["response_text"] == "Let me check that for you."
    assert result["tool_calls"] == [
        {
            "type": "tool_use",
            "id": "toolu-1",
            "name": "get_weather",
            "input": {"location": "SF"},
        }
    ]
    assert result["tool_names"] == ["get_weather"]


def test_normalize_portkey_row_chat_completions_with_function_tool_calls():
    row = _chat_completion_row(
        ai_org="openai",
        ai_model="gpt-4o-mini",
        response={
            "object": "chat.completion",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "The weather is sunny.",
                        "tool_calls": [
                            {
                                "id": "call-2",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"location": "SF"}',
                                },
                            }
                        ],
                    },
                    "index": 0,
                    "finish_reason": "tool_calls",
                }
            ],
        },
    )

    result = normalize_portkey_row(row)

    assert result["response_text"] == "The weather is sunny."
    assert result["tool_names"] == ["get_weather"]


def test_normalize_portkey_row_unrecognized_response_shape_falls_back_to_json_dump():
    row = _responses_row(response={"unexpected": "shape"})

    result = normalize_portkey_row(row)

    assert result["response_text"] == json.dumps({"unexpected": "shape"})
    assert result["tool_calls"] is None
    assert result["tool_names"] is None


def test_normalize_portkey_row_anthropic_extraction_skipped_when_object_present():
    response = {
        "object": "unexpected",
        "content": [{"type": "text", "text": "should not be used"}],
    }
    row = _responses_row(response=response)

    result = normalize_portkey_row(row)

    assert result["response_text"] == json.dumps(response)
    assert result["tool_calls"] is None


def test_normalize_portkey_row_anthropic_extraction_skipped_when_choices_present():
    response = {
        "choices": [],
        "content": [{"type": "text", "text": "should not be used"}],
    }
    row = _responses_row(response=response)

    result = normalize_portkey_row(row)

    assert result["response_text"] == json.dumps(response)
    assert result["tool_calls"] is None


def test_convert_portkey_export_streams_and_counts(tmp_path):
    input_path = tmp_path / "export.jsonl"
    good_1 = _responses_row(id="row-1", trace_id="trace-1")
    good_2 = _chat_completion_row(id="row-2", trace_id="trace-2")
    input_path.write_text(json.dumps(good_1) + "\n" + json.dumps(good_2) + "\n")
    output_path = tmp_path / "converted.jsonl"

    converted, skipped = convert_portkey_export(str(input_path), str(output_path))

    assert converted == 2
    assert skipped == 0
    lines = output_path.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["request_id"] == "row-1"
    assert json.loads(lines[1])["request_id"] == "row-2"


def test_convert_portkey_export_skips_malformed_json_line(tmp_path, capsys):
    input_path = tmp_path / "export.jsonl"
    good = _responses_row(id="row-good", trace_id="trace-good")
    input_path.write_text("not-json\n" + json.dumps(good) + "\n")
    output_path = tmp_path / "converted.jsonl"

    converted, skipped = convert_portkey_export(str(input_path), str(output_path))

    assert converted == 1
    assert skipped == 1
    captured = capsys.readouterr()
    assert "line 1" in captured.err
    assert good["request"]["input"] not in captured.err


def test_convert_portkey_export_rejects_row_missing_timestamp(tmp_path):
    input_path = tmp_path / "export.jsonl"
    bad = _responses_row(id="row-bad", trace_id="trace-bad")
    del bad["created_at"]
    good = _responses_row(id="row-good", trace_id="trace-good")
    input_path.write_text(json.dumps(bad) + "\n" + json.dumps(good) + "\n")
    output_path = tmp_path / "converted.jsonl"

    with pytest.raises(PortkeyConversionError, match="created_at"):
        convert_portkey_export(str(input_path), str(output_path))

    assert output_path.read_text() == ""


def test_convert_portkey_export_raises_oserror_on_missing_input(tmp_path):
    output_path = tmp_path / "converted.jsonl"

    with pytest.raises(OSError):
        convert_portkey_export(str(tmp_path / "nope.jsonl"), str(output_path))


def test_main_sync_portkey_missing_push_credential_returns_error(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("")
    export_file = tmp_path / "export.jsonl"
    export_file.write_text("")

    exit_code = main(["sync", "portkey", str(export_file), "--env-file", str(env_file)])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "METERGRAPH_APP_TOKEN" in captured.err


def test_main_sync_portkey_missing_export_file_returns_clean_error(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("METERGRAPH_APP_TOKEN=tok-123\n")
    missing_file = tmp_path / "nope.jsonl"

    exit_code = main(
        ["sync", "portkey", str(missing_file), "--env-file", str(env_file)]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("Error: ")
    assert "nope.jsonl" in captured.err
    assert "Traceback" not in captured.err


def test_main_sync_portkey_prints_converted_skipped_pushed_summary(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("METERGRAPH_APP_TOKEN=tok-123\n")
    export_file = tmp_path / "export.jsonl"
    export_file.write_text(
        json.dumps(_responses_row(id="row-1", trace_id="trace-1")) + "\n" + "not-json\n"
    )

    with patch("metergraphrelay.cli.push_file", return_value=(1, 0)):
        exit_code = main(
            ["sync", "portkey", str(export_file), "--env-file", str(env_file)]
        )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Converted 1" in captured.out
    assert "skipped 1" in captured.out
    assert "pushed 1" in captured.out


def test_main_sync_portkey_returns_error_when_push_fails(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("METERGRAPH_APP_TOKEN=tok-123\n")
    export_file = tmp_path / "export.jsonl"
    export_file.write_text(json.dumps(_responses_row()) + "\n")

    with patch("metergraphrelay.cli.push_file", return_value=(0, 1)):
        exit_code = main(
            ["sync", "portkey", str(export_file), "--env-file", str(env_file)]
        )

    assert exit_code == 1


def test_main_sync_portkey_no_output_deletes_temp_file_after_successful_push(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("METERGRAPH_APP_TOKEN=tok-123\n")
    export_file = tmp_path / "export.jsonl"
    export_file.write_text(json.dumps(_responses_row()) + "\n")

    with patch("metergraphrelay.cli.push_file", return_value=(1, 0)) as mock_push:
        exit_code = main(
            ["sync", "portkey", str(export_file), "--env-file", str(env_file)]
        )

    assert exit_code == 0
    pushed_path = mock_push.call_args.args[0]
    assert not os.path.exists(pushed_path)


def test_main_sync_portkey_no_output_deletes_temp_file_after_failed_push(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("METERGRAPH_APP_TOKEN=tok-123\n")
    export_file = tmp_path / "export.jsonl"
    export_file.write_text(json.dumps(_responses_row()) + "\n")

    with patch("metergraphrelay.cli.push_file", return_value=(0, 1)) as mock_push:
        exit_code = main(
            ["sync", "portkey", str(export_file), "--env-file", str(env_file)]
        )

    assert exit_code == 1
    pushed_path = mock_push.call_args.args[0]
    assert not os.path.exists(pushed_path)


def test_main_sync_portkey_output_retained_after_failed_push(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("METERGRAPH_APP_TOKEN=tok-123\n")
    export_file = tmp_path / "export.jsonl"
    export_file.write_text(json.dumps(_responses_row()) + "\n")
    output_path = tmp_path / "converted.jsonl"

    with patch("metergraphrelay.cli.push_file", return_value=(0, 1)):
        exit_code = main(
            [
                "sync",
                "portkey",
                str(export_file),
                "--output",
                str(output_path),
                "--env-file",
                str(env_file),
            ]
        )

    assert exit_code == 1
    assert output_path.exists()
    assert len(output_path.read_text().splitlines()) == 1


def test_main_sync_portkey_output_retained_and_empty_when_all_rows_malformed(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("METERGRAPH_APP_TOKEN=tok-123\n")
    export_file = tmp_path / "export.jsonl"
    export_file.write_text("not-json\n")
    output_path = tmp_path / "converted.jsonl"

    with patch("metergraphrelay.cli.push_file") as mock_push:
        exit_code = main(
            [
                "sync",
                "portkey",
                str(export_file),
                "--output",
                str(output_path),
                "--env-file",
                str(env_file),
            ]
        )

    assert exit_code == 0
    assert output_path.exists()
    assert output_path.read_text() == ""
    mock_push.assert_not_called()


def test_sync_portkey_help_documents_prerequisites(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["sync", "portkey", "--help"])

    help_text = " ".join(capsys.readouterr().out.split())
    for expected in [
        "Portkey subscription",
        "log export",
        "never contacts Portkey",
        "uploaded to MeterGraph",
        "--output",
        "--env-file",
    ]:
        assert expected in help_text, f"missing {expected!r} in --help output"


def test_sync_help_lists_portkey_subcommand(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["sync", "--help"])

    assert "portkey" in capsys.readouterr().out


def test_main_sync_portkey_returns_clean_error_on_invalid_utf8_export(
    tmp_path, capsys
):
    env_file = tmp_path / ".env"
    env_file.write_text("METERGRAPH_APP_TOKEN=tok-123\n")
    export_file = tmp_path / "export.jsonl"
    export_file.write_bytes(b"\xff\xfe\x00\x01\n")
    output_path = tmp_path / "converted.jsonl"

    exit_code = main(
        [
            "sync",
            "portkey",
            str(export_file),
            "--output",
            str(output_path),
            "--env-file",
            str(env_file),
        ]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("Error: ")
    assert "Traceback" not in captured.err
    assert not output_path.exists()


def test_main_sync_portkey_returns_clean_error_when_output_directory_missing(
    tmp_path, capsys
):
    env_file = tmp_path / ".env"
    env_file.write_text("METERGRAPH_APP_TOKEN=tok-123\n")
    export_file = tmp_path / "export.jsonl"
    export_file.write_text(json.dumps(_responses_row()) + "\n")
    output_path = tmp_path / "missing-dir" / "converted.jsonl"

    exit_code = main(
        [
            "sync",
            "portkey",
            str(export_file),
            "--output",
            str(output_path),
            "--env-file",
            str(env_file),
        ]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("Error: ")
    assert "Traceback" not in captured.err


def test_normalize_portkey_row_without_import_context_omits_import_fields():
    result = normalize_portkey_row(_responses_row())
    assert "import_source" not in result
    assert "import_source_scope" not in result
    assert "import_event_id" not in result


def test_normalize_portkey_row_with_import_context_adds_dedup_fields():
    ctx = ImportContext(source="portkey", source_scope="ws-acme")
    result = normalize_portkey_row(_responses_row(id="pk-req-1"), import_context=ctx)
    assert result["import_source"] == "portkey"
    assert result["import_source_scope"] == "ws-acme"
    assert result["import_event_id"] == "pk-req-1"
    # import_event_id is the stable Portkey request id, same value as request_id.
    assert result["import_event_id"] == result["request_id"]


def test_convert_portkey_export_threads_import_context_into_every_row(tmp_path):
    ctx = ImportContext(source="portkey", source_scope="ws-acme")
    input_path = tmp_path / "raw.jsonl"
    input_path.write_text(
        json.dumps(_responses_row(id="row-1", trace_id="t-1")) + "\n"
        + json.dumps(_chat_completion_row(id="row-2", trace_id="t-2")) + "\n"
    )
    output_path = tmp_path / "converted.jsonl"

    converted, skipped = convert_portkey_export(
        str(input_path), str(output_path), import_context=ctx
    )

    assert (converted, skipped) == (2, 0)
    rows = [json.loads(line) for line in output_path.read_text().splitlines()]
    assert [r["import_event_id"] for r in rows] == ["row-1", "row-2"]
    assert all(r["import_source"] == "portkey" for r in rows)
    assert all(r["import_source_scope"] == "ws-acme" for r in rows)


def test_convert_portkey_export_without_context_keeps_rows_free_of_import_fields(
    tmp_path,
):
    input_path = tmp_path / "raw.jsonl"
    input_path.write_text(json.dumps(_responses_row(id="row-1", trace_id="t-1")) + "\n")
    output_path = tmp_path / "converted.jsonl"

    convert_portkey_export(str(input_path), str(output_path))

    row = json.loads(output_path.read_text().splitlines()[0])
    assert "import_source" not in row


# --- API-mode import_event_id identity validation (metergraph-internal requires
# a stripped string of length 1..512; a bad id fails the async worker's batch
# after upload, so API mode must reject it locally and fail the window). ---

_IMPORT_CTX = ImportContext(source="portkey", source_scope="ws-acme")


def test_portkey_conversion_error_is_a_value_error():
    # Consistent with the module's existing convention of ValueError-family errors.
    assert issubclass(PortkeyConversionError, ValueError)


def test_normalize_portkey_row_import_mode_rejects_missing_id():
    row = _responses_row()
    del row["id"]
    with pytest.raises(PortkeyConversionError):
        normalize_portkey_row(row, import_context=_IMPORT_CTX)


def test_normalize_portkey_row_import_mode_rejects_none_id():
    with pytest.raises(PortkeyConversionError):
        normalize_portkey_row(_responses_row(id=None), import_context=_IMPORT_CTX)


def test_normalize_portkey_row_import_mode_rejects_blank_id():
    with pytest.raises(PortkeyConversionError):
        normalize_portkey_row(_responses_row(id=""), import_context=_IMPORT_CTX)


def test_normalize_portkey_row_import_mode_rejects_whitespace_only_id():
    with pytest.raises(PortkeyConversionError):
        normalize_portkey_row(_responses_row(id="   "), import_context=_IMPORT_CTX)


@pytest.mark.parametrize("bad_id", [123, 12.5, True, ["pk-1"], {"id": "pk-1"}])
def test_normalize_portkey_row_import_mode_rejects_non_string_id(bad_id):
    with pytest.raises(PortkeyConversionError):
        normalize_portkey_row(_responses_row(id=bad_id), import_context=_IMPORT_CTX)


def test_normalize_portkey_row_import_mode_rejects_id_longer_than_512():
    with pytest.raises(PortkeyConversionError):
        normalize_portkey_row(
            _responses_row(id="a" * 513), import_context=_IMPORT_CTX
        )


def test_normalize_portkey_row_import_mode_accepts_id_of_exactly_512():
    max_id = "a" * 512
    result = normalize_portkey_row(
        _responses_row(id=max_id), import_context=_IMPORT_CTX
    )
    assert result["import_event_id"] == max_id


def test_normalize_portkey_row_import_mode_strips_id_to_canonical_value():
    result = normalize_portkey_row(
        _responses_row(id="  pk-req-9  "), import_context=_IMPORT_CTX
    )
    assert result["import_event_id"] == "pk-req-9"


def test_convert_portkey_export_import_mode_fails_window_on_invalid_id(tmp_path):
    input_path = tmp_path / "raw.jsonl"
    input_path.write_text(
        json.dumps(_responses_row(id="row-1", trace_id="t-1")) + "\n"
        + json.dumps(_responses_row(id="", trace_id="t-2")) + "\n"
    )
    output_path = tmp_path / "converted.jsonl"

    # The bad imported id must fail the whole conversion/window, not be skipped —
    # a silent skip would let the server checkpoint complete missing records.
    with pytest.raises(PortkeyConversionError):
        convert_portkey_export(
            str(input_path), str(output_path), import_context=_IMPORT_CTX
        )


def test_convert_portkey_export_manual_mode_accepts_blank_id_unchanged(tmp_path):
    # Backward compatibility: the same blank id that fails API mode is passed
    # through untouched in manual mode (no import_context) — no new failures.
    input_path = tmp_path / "raw.jsonl"
    input_path.write_text(json.dumps(_responses_row(id="", trace_id="t-1")) + "\n")
    output_path = tmp_path / "converted.jsonl"

    converted, skipped = convert_portkey_export(str(input_path), str(output_path))

    assert (converted, skipped) == (1, 0)
    row = json.loads(output_path.read_text().splitlines()[0])
    assert row["request_id"] == ""
    assert "import_event_id" not in row


def test_main_sync_portkey_zero_converted_summary_reports_all_counts(
    tmp_path, capsys
):
    env_file = tmp_path / ".env"
    env_file.write_text("METERGRAPH_APP_TOKEN=tok-123\n")
    export_file = tmp_path / "export.jsonl"
    export_file.write_text("not-json\n")

    with patch("metergraphrelay.cli.push_file") as mock_push:
        exit_code = main(
            ["sync", "portkey", str(export_file), "--env-file", str(env_file)]
        )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Converted 0" in captured.out
    assert "skipped 1" in captured.out
    assert "pushed 0" in captured.out
    assert "0 failed" in captured.out
    mock_push.assert_not_called()


class _StopConvert(Exception):
    """Sentinel raised from on_progress to prove convert does not swallow it."""


def test_convert_portkey_export_invokes_on_progress_per_processed_row(tmp_path):
    input_path = tmp_path / "export.jsonl"
    good_1 = _responses_row(id="row-1", trace_id="trace-1")
    good_2 = _chat_completion_row(id="row-2", trace_id="trace-2")
    # A good row, a malformed line, another good row -> three processed lines.
    input_path.write_text(
        json.dumps(good_1) + "\n" + "not-json\n" + json.dumps(good_2) + "\n"
    )
    output_path = tmp_path / "converted.jsonl"
    ticks = []

    converted, skipped = convert_portkey_export(
        str(input_path), str(output_path), on_progress=lambda: ticks.append(1)
    )

    assert (converted, skipped) == (2, 1)
    assert len(ticks) == 3  # fires once per non-blank line, converted or skipped


def test_convert_portkey_export_without_on_progress_is_unchanged(tmp_path):
    input_path = tmp_path / "export.jsonl"
    input_path.write_text(json.dumps(_responses_row(id="row-1", trace_id="t-1")) + "\n")
    output_path = tmp_path / "converted.jsonl"

    converted, skipped = convert_portkey_export(str(input_path), str(output_path))

    assert (converted, skipped) == (1, 0)  # default None on_progress: unchanged


def test_convert_portkey_export_propagates_on_progress_exception(tmp_path):
    input_path = tmp_path / "export.jsonl"
    input_path.write_text(json.dumps(_responses_row(id="row-1", trace_id="t-1")) + "\n")
    output_path = tmp_path / "converted.jsonl"

    def _boom():
        raise _StopConvert()

    with pytest.raises(_StopConvert):
        convert_portkey_export(str(input_path), str(output_path), on_progress=_boom)
