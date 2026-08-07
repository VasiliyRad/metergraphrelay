import json

from metergraphrelay.providers.langfuse import build_base_params, build_filter


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


def test_build_base_params_includes_filter_when_selectors_given():
    params = build_base_params(
        until="2026-08-07T00:00:00+00:00",
        since=None,
        trace_names=["support-bot"],
        tags=["prod"],
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
            "type": "arrayOptions",
            "column": "tags",
            "operator": "all of",
            "value": ["prod"],
        },
    ]
