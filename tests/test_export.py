import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from metergraphrelay.export import normalize_completion, export_traces


def make_completion(**overrides):
    defaults = dict(
        id="chatcmpl-abc123",
        created=1780000000,
        model="gpt-4o-mini",
        metadata={"foo": "bar"},
        usage=SimpleNamespace(prompt_tokens=12, completion_tokens=34),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def make_message(role, content):
    return SimpleNamespace(role=role, content=content)


def test_normalize_completion_builds_expected_row():
    completion = make_completion()
    messages = [make_message("user", "hi"), make_message("assistant", "hello")]

    row = normalize_completion(completion, messages)

    assert row == {
        "id": "chatcmpl-abc123",
        "ts": "2026-05-28T20:26:40+00:00",
        "model": "gpt-4o-mini",
        "provider": "openai",
        "endpoint": "chat.completions",
        "status": "success",
        "input_tokens": 12,
        "output_tokens": 34,
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ],
        "metadata": {"foo": "bar"},
    }


def test_normalize_completion_handles_missing_usage_and_metadata():
    completion = make_completion(usage=None, metadata=None)

    row = normalize_completion(completion, [])

    assert row["input_tokens"] is None
    assert row["output_tokens"] is None
    assert row["metadata"] == {}
    assert row["messages"] == []


def test_normalize_completion_marks_error_status():
    completion = make_completion()

    row = normalize_completion(completion, [make_message("user", "hi")], error=RuntimeError("boom"))

    assert row["status"] == "error"
    assert row["messages"] == []
    assert row["input_tokens"] is None
    assert row["output_tokens"] is None


def test_export_traces_writes_jsonl_for_each_completion(tmp_path):
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
    client.chat.completions.messages.list.side_effect = (
        lambda completion_id: messages_by_id[completion_id]
    )
    output_path = tmp_path / "traces.jsonl"

    written = export_traces(client, count=2, output_path=str(output_path))

    client.chat.completions.list.assert_called_once_with(order="desc", limit=2)
    assert written == 2
    lines = output_path.read_text().splitlines()
    assert len(lines) == 2
    row1 = json.loads(lines[0])
    assert row1["id"] == "chatcmpl-1"
    assert row1["messages"] == [{"role": "user", "content": "hi"}]


def test_export_traces_empty_list_writes_empty_file(tmp_path):
    client = MagicMock()
    client.chat.completions.list.return_value = []
    output_path = tmp_path / "traces.jsonl"

    written = export_traces(client, count=5, output_path=str(output_path))

    assert written == 0
    assert output_path.read_text() == ""


def test_export_traces_marks_message_fetch_failure_as_error(tmp_path, capsys):
    client = MagicMock()
    completion = make_completion(id="chatcmpl-1", created=1780000000)
    client.chat.completions.list.return_value = [completion]
    client.chat.completions.messages.list.side_effect = RuntimeError("boom")
    output_path = tmp_path / "traces.jsonl"

    written = export_traces(client, count=1, output_path=str(output_path))

    assert written == 1
    row = json.loads(output_path.read_text().splitlines()[0])
    assert row["status"] == "error"
    assert row["messages"] == []
    captured = capsys.readouterr()
    assert "chatcmpl-1" in captured.err
    assert "boom" in captured.err


class _FakeAutoPaginatingPage:
    """Mimics a SyncCursorPage whose __iter__ keeps fetching beyond `limit`.

    Iterating this object never stops on its own (it behaves like the real
    OpenAI SDK page object, which auto-paginates past the requested page
    size). Only bounding the iteration with something like itertools.islice
    prevents it from exhausting the whole (simulated) account.
    """

    def __init__(self, total_items):
        self._total_items = total_items

    def __iter__(self):
        i = 0
        while i < self._total_items:
            yield make_completion(id=f"chatcmpl-{i}", created=1780000000 + i)
            i += 1


def test_export_traces_does_not_paginate_past_count(tmp_path):
    client = MagicMock()
    # Simulate an account with far more completions than requested.
    client.chat.completions.list.return_value = _FakeAutoPaginatingPage(total_items=100)
    client.chat.completions.messages.list.return_value = []
    output_path = tmp_path / "traces.jsonl"

    written = export_traces(client, count=10, output_path=str(output_path))

    client.chat.completions.list.assert_called_once_with(order="desc", limit=10)
    assert written == 10
    lines = output_path.read_text().splitlines()
    assert len(lines) == 10


def test_export_traces_echoes_to_stdout_when_enabled(tmp_path, capsys):
    client = MagicMock()
    completion = make_completion(id="chatcmpl-1", created=1780000000)
    client.chat.completions.list.return_value = [completion]
    client.chat.completions.messages.list.return_value = [make_message("user", "hi")]
    output_path = tmp_path / "traces.jsonl"

    export_traces(client, count=1, output_path=str(output_path), echo_stdout=True)

    captured = capsys.readouterr()
    assert "chatcmpl-1" in captured.out
