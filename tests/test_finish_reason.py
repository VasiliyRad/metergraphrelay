from metergraphrelay.finish_reason import (
    extract_phoenix_stop_reason,
    extract_stop_reason,
)


def test_extract_stop_reason_reads_common_provider_shapes():
    assert extract_stop_reason({"choices": [{"finish_reason": "length"}]}) == "length"
    assert extract_stop_reason({"stop_reason": "max_tokens"}) == "max_tokens"
    assert extract_stop_reason(
        {"incomplete_details": {"reason": "max_output_tokens"}}
    ) == "max_output_tokens"


def test_extract_stop_reason_reads_nested_known_response_containers():
    assert extract_stop_reason(
        {"extra": {"metadata": {"stop_reason": "length"}}}
    ) == "length"
    assert extract_stop_reason({"finish_reason": "   "}) is None


def test_extract_phoenix_stop_reason_reads_flattened_message_attribute():
    attributes = {"llm.output_messages.0.message.finish_reason": "length"}
    assert extract_phoenix_stop_reason(attributes) == "length"


def test_extract_phoenix_stop_reason_does_not_infer_from_token_attributes():
    attributes = {"llm.token_count.completion": 128}
    assert extract_phoenix_stop_reason(attributes) is None
