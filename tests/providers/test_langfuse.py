import base64
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from metergraphrelay.providers.langfuse import (
    LangfuseAPIError,
    RESPONSE_FIELDS,
    build_base_params,
    build_filter,
    fetch_observations_page,
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
            "https://cloud.langfuse.com", "pk-1", "sk-1", {"type": "GENERATION"}
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
            "pk-1",
            "sk-1",
            {"type": "GENERATION", "limit": "10"},
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
            "https://cloud.langfuse.com", "pk-1", "sk-1", {}
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
            fetch_observations_page("https://cloud.langfuse.com", "pk-1", "sk-1", {})


def test_fetch_observations_page_raises_on_network_error():
    with patch(
        "metergraphrelay.providers.langfuse.urllib.request.urlopen"
    ) as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.URLError("connection refused")
        with pytest.raises(LangfuseAPIError, match="connection refused"):
            fetch_observations_page("https://cloud.langfuse.com", "pk-1", "sk-1", {})


def test_fetch_observations_page_raises_on_malformed_json():
    with patch(
        "metergraphrelay.providers.langfuse.urllib.request.urlopen"
    ) as mock_urlopen:
        mock_urlopen.return_value = _mock_response(200, b"not json")
        with pytest.raises(LangfuseAPIError, match="invalid JSON"):
            fetch_observations_page("https://cloud.langfuse.com", "pk-1", "sk-1", {})


def test_fetch_observations_page_raises_when_response_missing_data_or_meta():
    body = json.dumps({"unexpected": "shape"}).encode()
    with patch(
        "metergraphrelay.providers.langfuse.urllib.request.urlopen"
    ) as mock_urlopen:
        mock_urlopen.return_value = _mock_response(200, body)
        with pytest.raises(LangfuseAPIError, match="v4"):
            fetch_observations_page("https://cloud.langfuse.com", "pk-1", "sk-1", {})
