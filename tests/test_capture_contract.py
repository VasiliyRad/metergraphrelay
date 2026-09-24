"""The capture contract, and the provider shapes that have to reach it.

These assertions restate metergraph-pipeline's
``trace_classification/capture_normalizer`` rules (``_tool_calls``,
``_normalize_result``). The pipeline is not importable here, so the contract is
duplicated deliberately; the pipeline remains the authority and this file is the
importer's copy of what it must satisfy.
"""

from __future__ import annotations

import json

import pytest

from metergraphrelay.capture_contract import (
    NATIVE_SEARCH_AUDIT_TYPES,
    capture_response_text,
    capture_text,
    capture_tool_call,
    capture_tool_calls,
)

CONTRACT_TOOL_CALL_FIELDS = {"call_id", "name", "arguments", "result", "status", "idempotency"}


def assert_capture_contract(row: dict) -> None:
    """Fail when a normalized row is one the pipeline would mark unusable."""
    response_text = row.get("response_text")
    assert isinstance(response_text, str), (
        "response_text must be a string; None marks the result malformed and "
        "drops the row from analysis"
    )
    tool_calls = row.get("tool_calls")
    if tool_calls is None:
        return
    assert isinstance(tool_calls, list) and tool_calls
    for call in tool_calls:
        assert isinstance(call, dict)
        if call.get("type") in NATIVE_SEARCH_AUDIT_TYPES:
            continue  # read as native-search audit, exempt from the shape
        assert set(call) <= CONTRACT_TOOL_CALL_FIELDS, (
            f"tool call carries provider wire fields: {sorted(set(call) - CONTRACT_TOOL_CALL_FIELDS)}"
        )
        assert isinstance(call.get("call_id"), str) and call["call_id"].strip()
        assert isinstance(call.get("name"), str) and call["name"].strip()
        assert isinstance(call.get("arguments"), str)


def test_capture_text_spells_an_absent_response_as_empty_string():
    assert capture_text([]) == ""
    assert capture_text(["a", "b"]) == "a\nb"


def test_anthropic_tool_use_becomes_the_contract_shape():
    call = capture_tool_call(
        {"type": "tool_use", "id": "toolu-1", "name": "emit", "input": {"score": 7}}
    )

    assert call == {"call_id": "toolu-1", "name": "emit", "arguments": '{"score": 7}'}
    assert json.loads(call["arguments"]) == {"score": 7}


def test_openai_responses_function_call_prefers_the_echoed_call_id():
    call = capture_tool_call(
        {
            "type": "function_call",
            "id": "fc-1",
            "call_id": "call-1",
            "name": "emit",
            "arguments": '{"a": 1}',
        }
    )

    assert call == {"call_id": "call-1", "name": "emit", "arguments": '{"a": 1}'}


def test_openai_chat_function_tool_call_becomes_the_contract_shape():
    call = capture_tool_call(
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "google_search", "arguments": '{"query": "cats"}'},
        }
    )

    assert call == {
        "call_id": "call-1",
        "name": "google_search",
        "arguments": '{"query": "cats"}',
    }


@pytest.mark.parametrize("audit_type", sorted(NATIVE_SEARCH_AUDIT_TYPES))
def test_native_search_audit_items_pass_through_unchanged(audit_type):
    item = {"type": audit_type, "id": "ws-1", "status": "completed"}

    assert capture_tool_call(item) is item


def test_items_with_no_contract_representation_are_dropped():
    # An OpenAI reasoning item is neither a tool call nor search audit. Passing
    # it through invalidates every tool call on the row and the response with
    # them, so it is dropped instead.
    assert capture_tool_call({"type": "reasoning", "id": "rs-1", "summary": []}) is None
    assert capture_tool_call({"type": "tool_use", "name": "emit"}) is None
    assert capture_tool_call({"type": "tool_use", "id": "t-1"}) is None
    assert capture_tool_call("not a mapping") is None


def test_a_dropped_item_does_not_discard_its_siblings():
    calls = capture_tool_calls(
        [
            {"type": "reasoning", "id": "rs-1", "summary": []},
            {"type": "web_search_call", "id": "ws-1", "status": "completed"},
            {"type": "tool_use", "id": "t-1", "name": "emit", "input": {}},
        ]
    )

    assert calls == [
        {"type": "web_search_call", "id": "ws-1", "status": "completed"},
        {"call_id": "t-1", "name": "emit", "arguments": "{}"},
    ]


def test_no_surviving_tool_calls_is_none_not_an_empty_list():
    assert capture_tool_calls([{"type": "reasoning", "id": "rs-1"}]) is None
    assert capture_tool_calls([]) is None
    assert capture_tool_calls(None) is None


def test_a_recognised_tool_call_with_no_id_is_identified_by_position():
    # Providers that log whatever an integration handed them sometimes omit the
    # id. Dropping the call would describe a tool turn as text alone.
    calls = capture_tool_calls(
        [
            {"type": "text", "text": "ignored"},
            {"type": "tool_use", "name": "search", "input": {}},
        ]
    )

    assert calls == [{"call_id": "tool-1", "name": "search", "arguments": "{}"}]


def test_a_tool_call_with_no_name_is_dropped_even_with_a_position():
    assert capture_tool_calls([{"type": "tool_use", "id": "t-1"}]) is None


def test_capture_response_text_spells_a_text_less_reply_as_empty():
    assert capture_response_text(None) == ""
    assert capture_response_text("hi") == "hi"
    assert capture_response_text({"a": 1}) == '{"a": 1}'


def test_capture_response_text_leaves_an_opted_out_row_without_content():
    # The pipeline reads None here as the content opt-out, which is the one
    # place None is correct.
    assert capture_response_text(None, content_opted_in=False) is None
    assert capture_response_text("hi", content_opted_in=False) is None
