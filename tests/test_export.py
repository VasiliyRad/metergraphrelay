from types import SimpleNamespace

from openai_exporter.export import normalize_completion


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
