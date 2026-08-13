import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from metergraphrelay import __version__
from metergraphrelay.providers.openai import normalize_completion, pull_openai


def make_completion(**overrides):
    defaults = dict(
        id="chatcmpl-abc123",
        created=1780000000,
        model="gpt-4o-mini",
        metadata={"foo": "bar"},
        usage=SimpleNamespace(prompt_tokens=12, completion_tokens=34),
        choices=[SimpleNamespace(message=SimpleNamespace(content="hello"))],
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def make_message(role, content):
    return SimpleNamespace(role=role, content=content)


def test_normalize_completion_with_content_included():
    completion = make_completion()
    messages = [make_message("user", "hi")]

    row = normalize_completion(
        completion, messages, route="openai/backfill", include_content=True
    )

    expected = {
        "ts": "2026-05-28T20:26:40+00:00",
        "provider": "openai",
        "model": "gpt-4o-mini",
        "status": "success",
        "endpoint": "chat.completions",
        "input_tokens": 12,
        "output_tokens": 34,
        "error": False,
        "error_type": None,
        "request_id": "chatcmpl-abc123",
        "tags": {"foo": "bar"},
        "route": "openai/backfill",
        "content_opted_in": True,
        "request_json": json.dumps([{"role": "user", "content": "hi"}]),
        "response_text": "hello",
        "sdk": "metergraphrelay",
        "sdk_version": __version__,
    }
    assert {key: row[key] for key in expected} == expected
    assert row["import_source"] == "openai"
    assert row["import_source_scope"] == "default"
    assert row["import_event_id"] == "chatcmpl-abc123"
    assert len(row["trace_id"]) == 32
    assert len(row["span_id"]) == 16


def test_normalize_completion_response_text_comes_from_completion_choices():
    """messages.list() only ever returns request/input messages in practice —
    even if an assistant-role message shows up in the passed-in list, it must
    not be used as response_text or stripped from request_json. The real
    reply lives on completion.choices[0].message.content."""
    completion = make_completion(
        choices=[SimpleNamespace(message=SimpleNamespace(content="real reply"))]
    )
    messages = [
        make_message("user", "hi"),
        make_message("assistant", "should not be used as response_text"),
    ]

    row = normalize_completion(
        completion, messages, route="openai/backfill", include_content=True
    )

    assert row["response_text"] == "real reply"
    assert row["request_json"] == json.dumps(
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "should not be used as response_text"},
        ]
    )


def test_normalize_completion_without_content_included():
    completion = make_completion()
    messages = [make_message("user", "hi"), make_message("assistant", "hello")]

    row = normalize_completion(
        completion, messages, route="openai/backfill", include_content=False
    )

    assert row["content_opted_in"] is False
    assert row["request_json"] is None
    assert row["response_text"] is None
    assert row["input_tokens"] == 12
    assert row["output_tokens"] == 34
    assert row["error"] is False
    assert row["error_type"] is None


def test_normalize_completion_handles_missing_usage_and_metadata():
    completion = make_completion(usage=None, metadata=None, choices=[])

    row = normalize_completion(
        completion, [], route="openai/backfill", include_content=True
    )

    assert row["input_tokens"] is None
    assert row["output_tokens"] is None
    assert row["tags"] == {}
    assert row["request_json"] == json.dumps([])
    assert row["response_text"] is None


def test_normalize_completion_keeps_success_and_tokens_on_content_fetch_error():
    """The completion itself succeeded; only the messages sub-fetch failed.

    Marking it as an error with null tokens would silently undercount real cost,
    so status/tokens stay real and the partial failure is signalled via
    error/error_type instead.
    """
    completion = make_completion()

    row = normalize_completion(
        completion,
        [make_message("user", "hi")],
        route="openai/backfill",
        include_content=True,
        content_fetch_error=RuntimeError("boom"),
    )

    assert row["status"] == "success"
    assert row["input_tokens"] == 12
    assert row["output_tokens"] == 34
    assert row["error"] is True
    assert row["error_type"] == "RuntimeError"
    assert row["content_opted_in"] is False
    assert row["request_json"] is None
    assert row["response_text"] is None


def test_pull_openai_writes_jsonl_for_each_completion(tmp_path):
    client = MagicMock()
    completions = [
        make_completion(id="chatcmpl-1", created=1780000000),
        make_completion(id="chatcmpl-2", created=1780000100),
    ]
    client.chat.completions.list.return_value = completions
    messages_by_id = {
        "chatcmpl-1": [make_message("user", "hi")],
        "chatcmpl-2": [make_message("user", "yo")],
    }
    client.chat.completions.messages.list.side_effect = lambda completion_id: (
        messages_by_id[completion_id]
    )
    output_path = tmp_path / "traces.jsonl"

    written = pull_openai(
        client,
        count=2,
        output_path=str(output_path),
        route="openai/backfill",
        include_content=True,
    )

    client.chat.completions.list.assert_called_once_with(order="desc", limit=2)
    assert written == 2
    lines = output_path.read_text().splitlines()
    assert len(lines) == 2
    row1 = json.loads(lines[0])
    assert row1["request_id"] == "chatcmpl-1"
    assert row1["route"] == "openai/backfill"


def test_pull_openai_empty_list_writes_empty_file(tmp_path):
    client = MagicMock()
    client.chat.completions.list.return_value = []
    output_path = tmp_path / "traces.jsonl"

    written = pull_openai(
        client,
        count=5,
        output_path=str(output_path),
        route="openai/backfill",
        include_content=True,
    )

    assert written == 0
    assert output_path.read_text() == ""


def test_pull_openai_flags_message_fetch_failure_without_losing_tokens(
    tmp_path, capsys
):
    client = MagicMock()
    completion = make_completion(id="chatcmpl-1", created=1780000000)
    client.chat.completions.list.return_value = [completion]
    client.chat.completions.messages.list.side_effect = RuntimeError("boom")
    output_path = tmp_path / "traces.jsonl"

    written = pull_openai(
        client,
        count=1,
        output_path=str(output_path),
        route="openai/backfill",
        include_content=True,
    )

    assert written == 1
    row = json.loads(output_path.read_text().splitlines()[0])
    assert row["status"] == "success"
    assert row["error"] is True
    assert row["error_type"] == "RuntimeError"
    assert row["input_tokens"] == 12
    assert row["output_tokens"] == 34
    assert row["content_opted_in"] is False
    captured = capsys.readouterr()
    assert "chatcmpl-1" in captured.err
    assert "boom" in captured.err


def test_pull_openai_skips_message_fetch_when_content_not_included(tmp_path):
    client = MagicMock()
    client.chat.completions.list.return_value = [
        make_completion(id="chatcmpl-1", created=1780000000),
        make_completion(id="chatcmpl-2", created=1780000100),
    ]
    output_path = tmp_path / "traces.jsonl"

    written = pull_openai(
        client,
        count=2,
        output_path=str(output_path),
        route="openai/backfill",
        include_content=False,
    )

    client.chat.completions.messages.list.assert_not_called()
    assert written == 2
    rows = [json.loads(line) for line in output_path.read_text().splitlines()]
    assert [row["request_id"] for row in rows] == ["chatcmpl-1", "chatcmpl-2"]
    for row in rows:
        assert row["status"] == "success"
        assert row["error"] is False
        assert row["error_type"] is None
        assert row["content_opted_in"] is False
        assert row["input_tokens"] == 12
        assert row["output_tokens"] == 34


class _FakeAutoPaginatingPage:
    """Mimics a SyncCursorPage whose __iter__ keeps fetching beyond `limit`."""

    def __init__(self, total_items):
        self._total_items = total_items

    def __iter__(self):
        i = 0
        while i < self._total_items:
            yield make_completion(id=f"chatcmpl-{i}", created=1780000000 + i)
            i += 1


def test_pull_openai_does_not_paginate_past_count(tmp_path):
    client = MagicMock()
    client.chat.completions.list.return_value = _FakeAutoPaginatingPage(total_items=100)
    client.chat.completions.messages.list.return_value = []
    output_path = tmp_path / "traces.jsonl"

    written = pull_openai(
        client,
        count=10,
        output_path=str(output_path),
        route="openai/backfill",
        include_content=True,
    )

    client.chat.completions.list.assert_called_once_with(order="desc", limit=10)
    assert written == 10


def test_pull_openai_echoes_to_stdout_when_enabled(tmp_path, capsys):
    client = MagicMock()
    completion = make_completion(id="chatcmpl-1", created=1780000000)
    client.chat.completions.list.return_value = [completion]
    client.chat.completions.messages.list.return_value = [make_message("user", "hi")]
    output_path = tmp_path / "traces.jsonl"

    pull_openai(
        client,
        count=1,
        output_path=str(output_path),
        route="openai/backfill",
        include_content=True,
        echo_stdout=True,
    )

    captured = capsys.readouterr()
    assert "chatcmpl-1" in captured.out
