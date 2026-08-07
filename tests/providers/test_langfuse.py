import base64
import json
import urllib.error
import urllib.parse
from unittest.mock import MagicMock, patch

import pytest

from metergraphrelay.providers.langfuse import (
    LangfuseAPIError,
    RESPONSE_FIELDS,
    _map_content,
    _response_text,
    build_base_params,
    build_filter,
    fetch_observations_page,
    infer_provider,
)


def test_build_filter_returns_none_when_no_selectors():
    assert build_filter([], []) is None


def test_build_filter_trace_name_uses_string_options_any_of():
    result = build_filter(["support-bot", "billing-bot"], [])
    assert json.loads(result) == [
        {
            "type": "stringOptions",
            "column": "traceName",
            "operator": "any of",
            "value": ["support-bot", "billing-bot"],
        }
    ]


def test_build_filter_tags_uses_array_options_all_of():
    result = build_filter([], ["prod", "tier-1"])
    assert json.loads(result) == [
        {
            "type": "arrayOptions",
            "column": "tags",
            "operator": "all of",
            "value": ["prod", "tier-1"],
        }
    ]


def test_build_filter_combines_trace_name_and_tags():
    result = build_filter(["support-bot"], ["prod"])
    assert json.loads(result) == [
        {
            "type": "stringOptions",
            "column": "traceName",
            "operator": "any of",
            "value": ["support-bot"],
        },
        {
            "type": "arrayOptions",
            "column": "tags",
            "operator": "all of",
            "value": ["prod"],
        },
    ]


def test_build_base_params_minimal_defaults():
    params = build_base_params(
        until="2026-08-07T00:00:00+00:00",
        since=None,
        trace_names=[],
        tags=[],
        environment=None,
    )
    assert params["type"] == "GENERATION"
    assert params["toStartTime"] == "2026-08-07T00:00:00+00:00"
    assert "fromStartTime" not in params
    assert "environment" not in params
    assert "filter" not in params


def test_build_base_params_includes_since_and_environment():
    params = build_base_params(
        until="2026-08-07T00:00:00+00:00",
        since="2026-08-01T00:00:00+00:00",
        trace_names=[],
        tags=[],
        environment="production",
    )
    assert params["fromStartTime"] == "2026-08-01T00:00:00+00:00"
    assert params["environment"] == "production"


def test_build_base_params_filter_includes_selector_and_safety_conditions():
    params = build_base_params(
        until="2026-08-07T00:00:00+00:00",
        since="2026-08-01T00:00:00+00:00",
        trace_names=["support-bot"],
        tags=["prod"],
        environment="production",
    )
    assert json.loads(params["filter"]) == [
        {
            "type": "stringOptions",
            "column": "traceName",
            "operator": "any of",
            "value": ["support-bot"],
        },
        {
            "type": "arrayOptions",
            "column": "tags",
            "operator": "all of",
            "value": ["prod"],
        },
        {
            "type": "stringOptions",
            "column": "type",
            "operator": "any of",
            "value": ["GENERATION"],
        },
        {
            "type": "datetime",
            "column": "startTime",
            "operator": ">=",
            "value": "2026-08-01T00:00:00+00:00",
        },
        {
            "type": "datetime",
            "column": "startTime",
            "operator": "<",
            "value": "2026-08-07T00:00:00+00:00",
        },
        {
            "type": "stringOptions",
            "column": "environment",
            "operator": "any of",
            "value": ["production"],
        },
    ]


def test_build_base_params_filter_omits_optional_safety_conditions_when_absent():
    params = build_base_params(
        until="2026-08-07T00:00:00+00:00",
        since=None,
        trace_names=["support-bot"],
        tags=[],
        environment=None,
    )
    assert json.loads(params["filter"]) == [
        {
            "type": "stringOptions",
            "column": "traceName",
            "operator": "any of",
            "value": ["support-bot"],
        },
        {
            "type": "stringOptions",
            "column": "type",
            "operator": "any of",
            "value": ["GENERATION"],
        },
        {
            "type": "datetime",
            "column": "startTime",
            "operator": "<",
            "value": "2026-08-07T00:00:00+00:00",
        },
    ]


def test_build_base_params_tags_only_triggers_filter_with_safety_conditions():
    params = build_base_params(
        until="2026-08-07T00:00:00+00:00",
        since=None,
        trace_names=[],
        tags=["prod"],
        environment=None,
    )
    assert json.loads(params["filter"]) == [
        {
            "type": "arrayOptions",
            "column": "tags",
            "operator": "all of",
            "value": ["prod"],
        },
        {
            "type": "stringOptions",
            "column": "type",
            "operator": "any of",
            "value": ["GENERATION"],
        },
        {
            "type": "datetime",
            "column": "startTime",
            "operator": "<",
            "value": "2026-08-07T00:00:00+00:00",
        },
    ]


def test_build_base_params_omits_individual_params_when_filter_present():
    params = build_base_params(
        until="2026-08-07T00:00:00+00:00",
        since="2026-08-01T00:00:00+00:00",
        trace_names=["support-bot"],
        tags=[],
        environment="production",
    )
    assert "type" not in params
    assert "toStartTime" not in params
    assert "fromStartTime" not in params
    assert "environment" not in params
    assert "filter" in params


def test_build_base_params_requests_metadata_field_group():
    params = build_base_params(
        until="2026-08-07T00:00:00+00:00",
        since=None,
        trace_names=[],
        tags=[],
        environment=None,
    )
    assert "metadata" in RESPONSE_FIELDS.split(",")
    assert params["fields"] == RESPONSE_FIELDS


def test_build_base_params_never_sends_deprecated_parse_io_as_json():
    params = build_base_params(
        until="2026-08-07T00:00:00+00:00",
        since=None,
        trace_names=[],
        tags=[],
        environment=None,
    )
    assert "parseIoAsJson" not in params


def _mock_response(status, body: bytes):
    response = MagicMock()
    response.status = status
    response.read.return_value = body
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    return response


def test_fetch_observations_page_sends_basic_auth_header():
    body = json.dumps({"data": [], "meta": {"cursor": None}}).encode()
    with patch(
        "metergraphrelay.providers.langfuse.urllib.request.urlopen"
    ) as mock_urlopen:
        mock_urlopen.return_value = _mock_response(200, body)
        fetch_observations_page(
            "https://cloud.langfuse.com",
            public_key="pk-1",
            secret_key="sk-1",
            params={"type": "GENERATION"},
        )

    request = mock_urlopen.call_args.args[0]
    expected = "Basic " + base64.b64encode(b"pk-1:sk-1").decode()
    assert request.get_header("Authorization") == expected


def test_fetch_observations_page_builds_correct_url():
    body = json.dumps({"data": [], "meta": {"cursor": None}}).encode()
    with patch(
        "metergraphrelay.providers.langfuse.urllib.request.urlopen"
    ) as mock_urlopen:
        mock_urlopen.return_value = _mock_response(200, body)
        fetch_observations_page(
            "https://cloud.langfuse.com",
            public_key="pk-1",
            secret_key="sk-1",
            params={"type": "GENERATION", "limit": "10"},
        )

    request = mock_urlopen.call_args.args[0]
    assert request.full_url == (
        "https://cloud.langfuse.com/api/public/v2/observations"
        "?type=GENERATION&limit=10"
    )


def test_fetch_observations_page_returns_parsed_payload():
    body = json.dumps({"data": [{"id": "obs-1"}], "meta": {"cursor": "abc"}}).encode()
    with patch(
        "metergraphrelay.providers.langfuse.urllib.request.urlopen"
    ) as mock_urlopen:
        mock_urlopen.return_value = _mock_response(200, body)
        payload = fetch_observations_page(
            "https://cloud.langfuse.com", public_key="pk-1", secret_key="sk-1", params={}
        )

    assert payload == {"data": [{"id": "obs-1"}], "meta": {"cursor": "abc"}}


def test_fetch_observations_page_raises_on_http_error():
    with patch(
        "metergraphrelay.providers.langfuse.urllib.request.urlopen"
    ) as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://cloud.langfuse.com/api/public/v2/observations",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=None,
        )
        with pytest.raises(LangfuseAPIError, match="401"):
            fetch_observations_page(
                "https://cloud.langfuse.com",
                public_key="pk-1",
                secret_key="sk-1",
                params={},
            )


def test_fetch_observations_page_raises_on_network_error():
    with patch(
        "metergraphrelay.providers.langfuse.urllib.request.urlopen"
    ) as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.URLError("connection refused")
        with pytest.raises(LangfuseAPIError, match="connection refused"):
            fetch_observations_page(
                "https://cloud.langfuse.com",
                public_key="pk-1",
                secret_key="sk-1",
                params={},
            )


def test_fetch_observations_page_raises_on_malformed_json():
    with patch(
        "metergraphrelay.providers.langfuse.urllib.request.urlopen"
    ) as mock_urlopen:
        mock_urlopen.return_value = _mock_response(200, b"not json")
        with pytest.raises(LangfuseAPIError, match="invalid JSON"):
            fetch_observations_page(
                "https://cloud.langfuse.com",
                public_key="pk-1",
                secret_key="sk-1",
                params={},
            )


def test_fetch_observations_page_raises_when_response_missing_data_or_meta():
    body = json.dumps({"unexpected": "shape"}).encode()
    with patch(
        "metergraphrelay.providers.langfuse.urllib.request.urlopen"
    ) as mock_urlopen:
        mock_urlopen.return_value = _mock_response(200, body)
        with pytest.raises(LangfuseAPIError, match="v4"):
            fetch_observations_page(
                "https://cloud.langfuse.com",
                public_key="pk-1",
                secret_key="sk-1",
                params={},
            )


def test_fetch_observations_page_raises_when_data_is_not_a_list():
    body = json.dumps({"data": {"not": "a list"}, "meta": {"cursor": None}}).encode()
    with patch(
        "metergraphrelay.providers.langfuse.urllib.request.urlopen"
    ) as mock_urlopen:
        mock_urlopen.return_value = _mock_response(200, body)
        with pytest.raises(LangfuseAPIError, match="v4"):
            fetch_observations_page(
                "https://cloud.langfuse.com",
                public_key="pk-1",
                secret_key="sk-1",
                params={},
            )


def test_fetch_observations_page_raises_when_meta_is_not_a_dict():
    body = json.dumps({"data": [], "meta": "not-a-dict"}).encode()
    with patch(
        "metergraphrelay.providers.langfuse.urllib.request.urlopen"
    ) as mock_urlopen:
        mock_urlopen.return_value = _mock_response(200, body)
        with pytest.raises(LangfuseAPIError, match="v4"):
            fetch_observations_page(
                "https://cloud.langfuse.com",
                public_key="pk-1",
                secret_key="sk-1",
                params={},
            )


def test_fetch_observations_page_rejects_positional_credentials():
    with patch("metergraphrelay.providers.langfuse.urllib.request.urlopen"):
        with pytest.raises(TypeError, match="positional argument"):
            fetch_observations_page(
                "https://cloud.langfuse.com", "pk-1", "sk-1", {"type": "GENERATION"}
            )


def test_fetch_observations_page_omits_dangling_question_mark_when_params_empty():
    body = json.dumps({"data": [], "meta": {"cursor": None}}).encode()
    with patch(
        "metergraphrelay.providers.langfuse.urllib.request.urlopen"
    ) as mock_urlopen:
        mock_urlopen.return_value = _mock_response(200, body)
        fetch_observations_page(
            "https://cloud.langfuse.com", public_key="pk-1", secret_key="sk-1", params={}
        )

    request = mock_urlopen.call_args.args[0]
    assert request.full_url == "https://cloud.langfuse.com/api/public/v2/observations"
    assert "?" not in request.full_url


def test_fetch_observations_page_url_encodes_filter_value_with_reserved_characters():
    body = json.dumps({"data": [], "meta": {"cursor": None}}).encode()
    filter_value = json.dumps(
        [
            {
                "type": "stringOptions",
                "column": "traceName",
                "operator": "any of",
                "value": ["support bot/v1"],
            }
        ]
    )
    with patch(
        "metergraphrelay.providers.langfuse.urllib.request.urlopen"
    ) as mock_urlopen:
        mock_urlopen.return_value = _mock_response(200, body)
        fetch_observations_page(
            "https://cloud.langfuse.com",
            public_key="pk-1",
            secret_key="sk-1",
            params={"filter": filter_value},
        )

    request = mock_urlopen.call_args.args[0]
    expected_query = urllib.parse.urlencode({"filter": filter_value})
    assert request.full_url == (
        f"https://cloud.langfuse.com/api/public/v2/observations?{expected_query}"
    )
    # Independent of the round-trip comparison above: prove specific reserved
    # characters were actually percent-encoded, not passed through raw.
    assert "%22" in request.full_url  # encoded "
    assert "%2F" in request.full_url  # encoded /
    assert "{" not in request.full_url
    assert "}" not in request.full_url
    assert " " not in request.full_url


def test_infer_provider_uses_explicit_metadata_when_present():
    observation = {
        "metadata": {"provider": "openai"},
        "providedModelName": "claude-3-opus",
    }
    assert infer_provider(observation) == "openai"


def test_infer_provider_falls_back_to_model_family_prefix_openai():
    assert infer_provider({"providedModelName": "gpt-4o-mini"}) == "openai"


def test_infer_provider_falls_back_to_model_family_prefix_anthropic():
    assert infer_provider({"providedModelName": "claude-3-opus"}) == "anthropic"


def test_infer_provider_returns_unknown_when_no_match():
    assert infer_provider({"providedModelName": "some-custom-model"}) == "unknown"


def test_infer_provider_returns_unknown_when_model_name_missing():
    assert infer_provider({}) == "unknown"


def test_infer_provider_ignores_non_dict_metadata():
    assert (
        infer_provider({"metadata": "not-a-dict", "providedModelName": "gpt-4o"})
        == "openai"
    )


def test_infer_provider_falls_back_to_model_family_prefix_openai_o1():
    assert infer_provider({"providedModelName": "o1-preview"}) == "openai"


def test_infer_provider_falls_back_to_model_family_prefix_openai_o3():
    assert infer_provider({"providedModelName": "o3-mini"}) == "openai"


def test_infer_provider_falls_back_to_model_family_prefix_openai_chatgpt():
    assert infer_provider({"providedModelName": "chatgpt-4o-latest"}) == "openai"


def test_infer_provider_falls_back_to_model_family_prefix_google():
    assert infer_provider({"providedModelName": "gemini-1.5-pro"}) == "google"


def test_infer_provider_explicit_metadata_takes_precedence_over_model_prefix():
    observation = {
        "metadata": {"provider": "custom-provider"},
        "providedModelName": "gpt-4o-mini",
    }
    assert infer_provider(observation) == "custom-provider"


@pytest.mark.parametrize(
    "malformed_provider, model_name, expected_provider",
    [
        (42, "gpt-4o-mini", "openai"),
        (["openai"], "claude-3-opus", "anthropic"),
        ({"name": "openai"}, "gemini-pro", "google"),
    ],
)
def test_infer_provider_ignores_malformed_explicit_provider(
    malformed_provider, model_name, expected_provider
):
    observation = {
        "metadata": {"provider": malformed_provider},
        "providedModelName": model_name,
    }
    assert infer_provider(observation) == expected_provider


def test_infer_provider_malformed_explicit_provider_and_no_model_match_returns_unknown():
    observation = {"metadata": {"provider": 42}, "providedModelName": "some-custom-model"}
    assert infer_provider(observation) == "unknown"


def test_infer_provider_ignores_whitespace_only_explicit_provider():
    observation = {"metadata": {"provider": "   "}, "providedModelName": "gpt-4o-mini"}
    assert infer_provider(observation) == "openai"


def test_infer_provider_strips_whitespace_from_explicit_provider():
    observation = {
        "metadata": {"provider": "  openai  "},
        "providedModelName": "claude-3-opus",
    }
    assert infer_provider(observation) == "openai"


def test_infer_provider_handles_non_string_provided_model_name_int():
    assert infer_provider({"providedModelName": 12345}) == "unknown"


def test_infer_provider_handles_non_string_provided_model_name_list():
    assert infer_provider({"providedModelName": ["gpt-4o-mini"]}) == "unknown"


def test_infer_provider_valid_explicit_provider_short_circuits_before_model_name_check():
    observation = {
        "metadata": {"provider": "openai"},
        "providedModelName": {"unexpected": "shape"},
    }
    assert infer_provider(observation) == "openai"


def test_infer_provider_normalizes_explicit_provider_case():
    observation = {
        "metadata": {"provider": "OpenAI"},
        "providedModelName": "claude-3-opus",
    }
    assert infer_provider(observation) == "openai"


def test_infer_provider_normalizes_custom_explicit_provider_case():
    observation = {
        "metadata": {"provider": "Custom-Provider"},
        "providedModelName": "gpt-4o-mini",
    }
    assert infer_provider(observation) == "custom-provider"


def test_infer_provider_normalizes_explicit_provider_whitespace_and_case():
    observation = {
        "metadata": {"provider": "  OpenAI  "},
        "providedModelName": "claude-3-opus",
    }
    assert infer_provider(observation) == "openai"


def test_map_content_chat_message_list_becomes_request_json():
    result = _map_content([{"role": "user", "content": "hi"}])
    assert result == (json.dumps([{"role": "user", "content": "hi"}]), None)


def test_map_content_string_becomes_request_text():
    assert _map_content("plain prompt text") == (None, "plain prompt text")


def test_map_content_none_stays_none():
    assert _map_content(None) == (None, None)


def test_map_content_arbitrary_dict_becomes_request_text_as_json():
    result = _map_content({"foo": "bar"})
    assert result == (None, json.dumps({"foo": "bar"}))


def test_map_content_non_message_list_becomes_request_text_as_json():
    result = _map_content([1, 2, 3])
    assert result == (None, json.dumps([1, 2, 3]))


def test_response_text_passes_through_string():
    assert _response_text("the reply") == "the reply"


def test_response_text_serializes_non_string():
    assert _response_text({"foo": "bar"}) == json.dumps({"foo": "bar"})


def test_response_text_none_stays_none():
    assert _response_text(None) is None


def test_map_content_raw_json_chat_message_string_becomes_canonical_request_json():
    raw = '[{"role":   "user",  "content": "hi"}]'  # unusual spacing on purpose
    result = _map_content(raw)
    assert result == (json.dumps([{"role": "user", "content": "hi"}]), None)
    assert result[0] != raw  # proves canonical re-serialization, not passthrough


def test_map_content_raw_json_non_chat_object_string_stays_byte_for_byte():
    raw = '{"foo": "bar"}'
    assert _map_content(raw) == (None, raw)


def test_map_content_raw_json_non_message_list_string_stays_byte_for_byte():
    raw = "[1, 2, 3]"
    assert _map_content(raw) == (None, raw)


def test_map_content_malformed_json_string_stays_byte_for_byte():
    raw = '{"role": "user", "content": '  # truncated/invalid JSON
    assert _map_content(raw) == (None, raw)


def test_response_text_serializes_list():
    assert _response_text([1, 2, 3]) == json.dumps([1, 2, 3])


def test_response_text_serializes_number():
    assert _response_text(42) == json.dumps(42)


def test_response_text_serializes_bool():
    assert _response_text(True) == json.dumps(True)


def test_map_content_raw_json_null_string_stays_byte_for_byte():
    raw = "null"
    assert _map_content(raw) == (None, raw)


def test_map_content_raw_json_empty_list_string_stays_byte_for_byte():
    raw = "[]"
    assert _map_content(raw) == (None, raw)


def test_map_content_raw_json_mixed_valid_invalid_message_list_stays_byte_for_byte():
    raw = json.dumps([{"role": "user", "content": "hi"}, "not-a-message"])
    assert _map_content(raw) == (None, raw)


def test_map_content_empty_list_value_becomes_request_text_as_json():
    result = _map_content([])
    assert result == (None, json.dumps([]))


def test_map_content_mixed_valid_invalid_message_list_value_becomes_request_text_as_json():
    mixed = [{"role": "user", "content": "hi"}, "not-a-dict"]
    result = _map_content(mixed)
    assert result == (None, json.dumps(mixed))
