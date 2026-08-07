import json

from metergraphrelay.providers.langfuse import (
    RESPONSE_FIELDS,
    build_base_params,
    build_filter,
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
