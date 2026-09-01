import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from metergraphrelay import __version__
from metergraphrelay.providers.braintrust import (
    DEFAULT_BRAINTRUST_URL,
    PAGE_LIMIT,
    PREVIEW_LENGTH_UNTRUNCATED,
    BraintrustAPIError,
    _extract_output,
    _map_content,
    _sql_string,
    build_query,
    fetch_spans_page,
    infer_provider,
    map_metrics,
    normalize_span,
    pull_braintrust,
)


def make_span(**overrides):
    span = {
        "id": "span-row-1",
        "created": "2026-08-05T12:00:00+00:00",
        "span_id": "span-1",
        "root_span_id": "trace-1",
        "span_parents": ["trace-1"],
        "span_attributes": {"name": "OpenAI Chat Completion", "type": "llm"},
        "input": [{"role": "user", "content": "hi"}],
        "output": {"role": "assistant", "content": "hello"},
        "error": None,
        "metadata": {"model": "gpt-4o-mini"},
        "metrics": {
            "start": 1754395200.0,
            "end": 1754395201.5,
            "prompt_tokens": 12,
            "completion_tokens": 34,
        },
        "tags": [],
        "project_id": "proj-uuid-1",
        "estimated_cost": 0.00042,
    }
    span.update(overrides)
    return span


# -- SQL literal quoting ----------------------------------------------------


def test_sql_string_quotes_a_plain_value():
    assert _sql_string("my-project") == "'my-project'"


def test_sql_string_doubles_embedded_single_quotes():
    assert _sql_string("o'brien") == "'o''brien'"


@pytest.mark.parametrize("value", ["back\\slash", "trailing\\", "nul\x00byte"])
def test_sql_string_rejects_backslash_and_nul(value):
    with pytest.raises(BraintrustAPIError):
        _sql_string(value)


@pytest.mark.parametrize("value", ["", None, 5])
def test_sql_string_rejects_non_string_or_empty(value):
    with pytest.raises(BraintrustAPIError):
        _sql_string(value)


# -- query building ---------------------------------------------------------


def _query(**overrides):
    kwargs = dict(
        projects=["my-project"],
        since=None,
        until="2026-09-01T00:00:00+00:00",
        limit=100,
    )
    kwargs.update(overrides)
    return build_query(**kwargs)


def test_build_query_filters_to_llm_spans_on_the_spans_shape():
    query = _query()
    assert "FROM project_logs('my-project', shape => 'spans')" in query
    assert "WHERE span_attributes.type = 'llm'" in query


def test_build_query_always_bounds_by_until():
    assert "AND created < '2026-09-01T00:00:00+00:00'" in _query()


def test_build_query_omits_since_when_not_given():
    assert "created >=" not in _query()


def test_build_query_includes_since_when_given():
    query = _query(since="2026-08-01T00:00:00+00:00")
    assert "AND created >= '2026-08-01T00:00:00+00:00'" in query


def test_build_query_lists_every_project():
    query = _query(projects=["proj-a", "proj-b"])
    assert "FROM project_logs('proj-a', 'proj-b', shape => 'spans')" in query


def test_build_query_escapes_project_names():
    assert "project_logs('o''brien'," in _query(projects=["o'brien"])


def test_build_query_rejects_an_empty_project_list():
    with pytest.raises(BraintrustAPIError):
        _query(projects=[])


def test_build_query_sorts_by_pagination_key_for_cursor_compatibility():
    assert "ORDER BY _pagination_key DESC" in _query()


def test_build_query_omits_offset_on_the_first_page():
    assert "OFFSET" not in _query()


def test_build_query_passes_cursor_as_a_quoted_offset():
    assert "OFFSET 'cursor-token'" in _query(cursor="cursor-token")


def test_build_query_disables_preview_truncation_last():
    query = _query()
    assert query.rstrip().endswith(
        f"SETTINGS preview_length = {PREVIEW_LENGTH_UNTRUNCATED}"
    )


def test_build_query_selects_estimated_cost_and_never_scores():
    query = _query()
    assert "estimated_cost() AS estimated_cost" in query
    assert "scores" not in query


# -- HTTP -------------------------------------------------------------------


def _mock_response(status, body: bytes, headers=None):
    response = MagicMock()
    response.status = status
    response.read.return_value = body
    response.headers = headers if headers is not None else {}
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    return response


def _patch_urlopen(body, headers=None):
    return patch(
        "metergraphrelay.providers.braintrust.urllib.request.urlopen",
        return_value=_mock_response(200, body, headers),
    )


def test_fetch_spans_page_posts_bearer_auth_and_json_body():
    body = json.dumps({"data": []}).encode()
    with _patch_urlopen(body) as mock_urlopen:
        fetch_spans_page(DEFAULT_BRAINTRUST_URL, api_key="bt-key", query="SELECT 1")

    request = mock_urlopen.call_args.args[0]
    assert request.full_url == "https://api.braintrust.dev/btql"
    assert request.method == "POST"
    assert request.get_header("Authorization") == "Bearer bt-key"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data) == {"query": "SELECT 1", "fmt": "json"}


def test_fetch_spans_page_strips_a_trailing_slash_from_the_base_url():
    body = json.dumps({"data": []}).encode()
    with _patch_urlopen(body) as mock_urlopen:
        fetch_spans_page(
            "https://api-eu.braintrust.dev/", api_key="bt-key", query="SELECT 1"
        )

    assert mock_urlopen.call_args.args[0].full_url == (
        "https://api-eu.braintrust.dev/btql"
    )


def test_fetch_spans_page_returns_rows_and_no_cursor_by_default():
    body = json.dumps({"data": [{"id": "a"}], "schema": {}}).encode()
    with _patch_urlopen(body):
        rows, cursor = fetch_spans_page(
            DEFAULT_BRAINTRUST_URL, api_key="bt-key", query="SELECT 1"
        )

    assert rows == [{"id": "a"}]
    assert cursor is None


def test_fetch_spans_page_reads_the_cursor_header():
    body = json.dumps({"data": [{"id": "a"}]}).encode()
    with _patch_urlopen(body, {"x-bt-cursor": "next-token"}):
        _, cursor = fetch_spans_page(
            DEFAULT_BRAINTRUST_URL, api_key="bt-key", query="SELECT 1"
        )

    assert cursor == "next-token"


def test_fetch_spans_page_falls_back_to_the_s3_metadata_cursor_header():
    body = json.dumps({"data": [{"id": "a"}]}).encode()
    with _patch_urlopen(body, {"x-amz-meta-bt_cursor": "next-token"}):
        _, cursor = fetch_spans_page(
            DEFAULT_BRAINTRUST_URL, api_key="bt-key", query="SELECT 1"
        )

    assert cursor == "next-token"


def test_fetch_spans_page_treats_a_blank_cursor_header_as_no_cursor():
    body = json.dumps({"data": [{"id": "a"}]}).encode()
    with _patch_urlopen(body, {"x-bt-cursor": "   "}):
        _, cursor = fetch_spans_page(
            DEFAULT_BRAINTRUST_URL, api_key="bt-key", query="SELECT 1"
        )

    assert cursor is None


def test_fetch_spans_page_raises_on_http_error():
    with patch(
        "metergraphrelay.providers.braintrust.urllib.request.urlopen"
    ) as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://api.braintrust.dev/btql", 401, "Unauthorized", {}, None
        )
        with pytest.raises(BraintrustAPIError) as exc_info:
            fetch_spans_page(
                DEFAULT_BRAINTRUST_URL, api_key="bt-key", query="SELECT 1"
            )

    assert "HTTP 401" in str(exc_info.value)


def test_fetch_spans_page_raises_on_network_error():
    with patch(
        "metergraphrelay.providers.braintrust.urllib.request.urlopen"
    ) as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.URLError("connection refused")
        with pytest.raises(BraintrustAPIError):
            fetch_spans_page(
                DEFAULT_BRAINTRUST_URL, api_key="bt-key", query="SELECT 1"
            )


def test_fetch_spans_page_raises_on_malformed_json():
    with _patch_urlopen(b"not json"):
        with pytest.raises(BraintrustAPIError) as exc_info:
            fetch_spans_page(
                DEFAULT_BRAINTRUST_URL, api_key="bt-key", query="SELECT 1"
            )

    assert "invalid JSON" in str(exc_info.value)


@pytest.mark.parametrize("payload", [{"rows": []}, {"data": {"id": "a"}}, ["a"]])
def test_fetch_spans_page_raises_when_data_is_missing_or_not_a_list(payload):
    with _patch_urlopen(json.dumps(payload).encode()):
        with pytest.raises(BraintrustAPIError) as exc_info:
            fetch_spans_page(
                DEFAULT_BRAINTRUST_URL, api_key="bt-key", query="SELECT 1"
            )

    assert "'data'" in str(exc_info.value)


def test_fetch_spans_page_rejects_positional_credentials():
    with patch("metergraphrelay.providers.braintrust.urllib.request.urlopen"):
        with pytest.raises(TypeError):
            fetch_spans_page(DEFAULT_BRAINTRUST_URL, "bt-key", "SELECT 1")


# -- provider inference -----------------------------------------------------


def test_infer_provider_prefers_explicit_metadata_provider():
    span = make_span(metadata={"provider": "  Azure  ", "model": "gpt-4o-mini"})
    assert infer_provider(span) == "azure"


def test_infer_provider_falls_back_to_the_model_prefix():
    assert infer_provider(make_span(metadata={"model": "claude-3-5-haiku"})) == (
        "anthropic"
    )


def test_infer_provider_returns_unknown_without_provider_or_known_model():
    assert infer_provider(make_span(metadata={"model": "llama-3"})) == "unknown"


def test_infer_provider_tolerates_a_non_dict_metadata():
    assert infer_provider(make_span(metadata="oops")) == "unknown"


# -- content mapping --------------------------------------------------------


def test_map_content_keeps_a_chat_message_list_as_request_json():
    messages = [{"role": "user", "content": "hi"}]
    request_json, request_text = _map_content(messages)
    assert json.loads(request_json) == messages
    assert request_text is None


def test_map_content_parses_a_json_encoded_chat_message_list():
    messages = [{"role": "user", "content": "hi"}]
    request_json, request_text = _map_content(json.dumps(messages))
    assert json.loads(request_json) == messages
    assert request_text is None


def test_map_content_keeps_a_plain_string_as_request_text():
    assert _map_content("just text") == (None, "just text")


def test_map_content_serializes_any_other_shape_into_request_text():
    request_json, request_text = _map_content({"question": "why"})
    assert request_json is None
    assert json.loads(request_text) == {"question": "why"}


def test_map_content_maps_none_to_both_none():
    assert _map_content(None) == (None, None)


def test_extract_output_reads_an_assistant_message_object():
    assert _extract_output({"role": "assistant", "content": "hello"}) == (
        "hello",
        None,
    )


def test_extract_output_reads_a_plain_string():
    assert _extract_output("hello") == ("hello", None)


def test_extract_output_keeps_tool_calls_from_a_message_object():
    tool_calls = [{"id": "c1", "function": {"name": "lookup", "arguments": "{}"}}]
    text, calls = _extract_output(
        {"role": "assistant", "content": None, "tool_calls": tool_calls}
    )
    assert text is None
    assert calls == tool_calls


def test_extract_output_reads_anthropic_content_blocks():
    text, calls = _extract_output(
        [
            {"type": "text", "text": "thinking out loud"},
            {"type": "tool_use", "name": "search", "input": {}},
        ]
    )
    assert text == "thinking out loud"
    assert calls == [{"type": "tool_use", "name": "search", "input": {}}]


def test_extract_output_serializes_an_unrecognized_shape():
    text, calls = _extract_output({"answer": 4})
    assert json.loads(text) == {"answer": 4}
    assert calls is None


def test_extract_output_maps_none_to_both_none():
    assert _extract_output(None) == (None, None)


# -- metrics mapping --------------------------------------------------------


def test_map_metrics_reads_braintrust_normalized_token_names():
    usage = map_metrics(
        {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_cached_tokens": 60,
            "prompt_cache_creation_tokens": 10,
            "completion_reasoning_tokens": 5,
        }
    )
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 20
    assert usage["cache_read_tokens"] == 60
    assert usage["cache_write_tokens"] == 10
    assert usage["reasoning_tokens"] == 5


def test_map_metrics_does_not_add_cache_details_back_into_the_input_total():
    # Braintrust's prompt_tokens already includes cache reads and writes, which
    # is metergraph's convention too — unlike Langfuse's flattened shape, no
    # add-back is correct here.
    usage = map_metrics(
        {"prompt_tokens": 100, "prompt_cached_tokens": 60, "completion_tokens": 20}
    )
    assert usage["input_tokens"] == 100


def test_map_metrics_sums_ttl_split_cache_writes_when_the_aggregate_is_absent():
    usage = map_metrics(
        {
            "prompt_tokens": 100,
            "prompt_cache_creation_5m_tokens": 7,
            "prompt_cache_creation_1h_tokens": 3,
        }
    )
    assert usage["cache_write_tokens"] == 10


def test_map_metrics_prefers_the_aggregate_cache_write_over_the_ttl_split():
    usage = map_metrics(
        {
            "prompt_cache_creation_tokens": 12,
            "prompt_cache_creation_5m_tokens": 7,
        }
    )
    assert usage["cache_write_tokens"] == 12


def test_map_metrics_accepts_provider_native_token_names():
    usage = map_metrics(
        {
            "input_tokens": 11,
            "output_tokens": 22,
            "cache_read_input_tokens": 3,
            "cache_creation_input_tokens": 4,
            "reasoning_tokens": 5,
        }
    )
    assert usage["input_tokens"] == 11
    assert usage["output_tokens"] == 22
    assert usage["cache_read_tokens"] == 3
    assert usage["cache_write_tokens"] == 4
    assert usage["reasoning_tokens"] == 5


def test_map_metrics_ignores_booleans_which_are_int_subclasses():
    assert map_metrics({"prompt_tokens": True})["input_tokens"] is None


def test_map_metrics_derives_latency_from_start_and_end():
    assert map_metrics({"start": 100.0, "end": 101.25})["latency_ms"] == 1250


def test_map_metrics_omits_latency_when_end_precedes_start():
    assert map_metrics({"start": 200.0, "end": 100.0})["latency_ms"] is None


def test_map_metrics_omits_latency_when_a_bound_is_missing():
    assert map_metrics({"start": 100.0})["latency_ms"] is None


def test_map_metrics_returns_all_none_for_a_non_dict():
    assert map_metrics(None) == {
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "reasoning_tokens": None,
        "latency_ms": None,
    }


# -- normalization ----------------------------------------------------------


def test_normalize_span_maps_the_core_fields():
    row = normalize_span(make_span(), route_override=None)

    assert row["ts"] == "2026-08-05T12:00:00+00:00"
    assert row["source"] == "braintrust"
    assert row["sdk"] == "metergraphrelay"
    assert row["sdk_version"] == __version__
    assert row["provider"] == "openai"
    assert row["model"] == "gpt-4o-mini"
    assert row["status"] == "success"
    assert row["error"] is False
    assert row["error_type"] is None
    assert row["request_id"] == "span-row-1"
    assert row["span_id"] == "span-1"
    assert row["trace_id"] == "trace-1"
    assert row["parent_span_id"] == "trace-1"
    assert row["input_tokens"] == 12
    assert row["output_tokens"] == 34
    assert row["latency_ms"] == 1500
    assert row["cost_usd"] == 0.00042
    assert row["content_opted_in"] is True


def test_normalize_span_routes_to_the_span_name_by_default():
    row = normalize_span(make_span(), route_override=None)
    assert row["route"] == "OpenAI Chat Completion"
    assert "name" not in row["tags"]


def test_normalize_span_falls_back_to_a_static_route_without_a_span_name():
    row = normalize_span(
        make_span(span_attributes={"type": "llm"}), route_override=None
    )
    assert row["route"] == "braintrust/backfill"


def test_normalize_span_route_override_moves_the_name_into_tags():
    row = normalize_span(make_span(), route_override="my-app/support-bot")
    assert row["route"] == "my-app/support-bot"
    assert row["tags"]["name"] == "OpenAI Chat Completion"


def test_normalize_span_carries_tags_and_project_id():
    row = normalize_span(make_span(tags=["prod", "tier-1"]), route_override=None)
    assert row["tags"]["braintrust_tags"] == ["prod", "tier-1"]
    assert row["tags"]["braintrust_project_id"] == "proj-uuid-1"


def test_normalize_span_omits_empty_tag_list():
    row = normalize_span(make_span(tags=[]), route_override=None)
    assert "braintrust_tags" not in row["tags"]


def test_normalize_span_flags_a_string_error():
    row = normalize_span(make_span(error="Input too long"), route_override=None)
    assert row["error"] is True
    assert row["status"] == "error"
    assert row["error_type"] == "Input too long"


def test_normalize_span_reads_the_message_from_a_structured_error():
    row = normalize_span(
        make_span(error={"message": "rate limited", "code": 429}),
        route_override=None,
    )
    assert row["error_type"] == "rate limited"


def test_normalize_span_serializes_a_structured_error_without_a_message():
    row = normalize_span(make_span(error={"code": 429}), route_override=None)
    assert json.loads(row["error_type"]) == {"code": 429}


def test_normalize_span_treats_a_blank_error_string_as_no_error():
    row = normalize_span(make_span(error="   "), route_override=None)
    assert row["error"] is False
    assert row["error_type"] is None


def test_normalize_span_splits_input_into_request_json_and_text():
    row = normalize_span(make_span(), route_override=None)
    assert json.loads(row["request_json"]) == [{"role": "user", "content": "hi"}]
    assert row["request_text"] is None
    assert row["response_text"] == "hello"


def test_normalize_span_records_tool_calls_and_names():
    tool_calls = [{"id": "c1", "function": {"name": "lookup", "arguments": "{}"}}]
    row = normalize_span(
        make_span(
            output={"role": "assistant", "content": None, "tool_calls": tool_calls}
        ),
        route_override=None,
    )
    assert row["tool_calls"] == tool_calls
    assert row["tool_names"] == ["lookup"]


def test_normalize_span_falls_back_to_the_row_id_when_span_id_is_absent():
    span = make_span()
    del span["span_id"]
    assert normalize_span(span, route_override=None)["span_id"] == "span-row-1"


def test_normalize_span_has_no_parent_for_a_root_span():
    row = normalize_span(make_span(span_parents=[]), route_override=None)
    assert row["parent_span_id"] is None


def test_normalize_span_reads_cost_from_metrics_when_the_column_is_absent():
    span = make_span(metrics={"estimated_cost": 0.5})
    del span["estimated_cost"]
    assert normalize_span(span, route_override=None)["cost_usd"] == 0.5


def test_normalize_span_never_carries_scores():
    row = normalize_span(make_span(scores={"Factuality": 1.0}), route_override=None)
    assert "scores" not in row


@pytest.mark.parametrize("missing", ["id", "created"])
def test_normalize_span_raises_key_error_on_an_identity_field(missing):
    span = make_span()
    del span[missing]
    with pytest.raises(KeyError):
        normalize_span(span, route_override=None)


# -- pull -------------------------------------------------------------------


def _call_pull_braintrust(output_path, **overrides):
    kwargs = dict(
        base_url=DEFAULT_BRAINTRUST_URL,
        api_key="bt-key",
        projects=["my-project"],
        count=10,
        since=None,
        until="2026-09-01T00:00:00+00:00",
        route=None,
        output_path=str(output_path),
    )
    kwargs.update(overrides)
    return pull_braintrust(**kwargs)


def test_pull_braintrust_single_page_under_count(tmp_path):
    output_path = tmp_path / "traces.jsonl"
    spans = [make_span(id=f"span-{i}") for i in range(3)]

    with patch(
        "metergraphrelay.providers.braintrust.fetch_spans_page",
        return_value=(spans, None),
    ) as mock_fetch:
        imported, skipped = _call_pull_braintrust(output_path)

    assert (imported, skipped) == (3, 0)
    mock_fetch.assert_called_once()
    assert len(output_path.read_text().splitlines()) == 3


def test_pull_braintrust_stops_at_count_cap_mid_page(tmp_path):
    output_path = tmp_path / "traces.jsonl"
    spans = [make_span(id=f"span-{i}") for i in range(5)]

    with patch(
        "metergraphrelay.providers.braintrust.fetch_spans_page",
        return_value=(spans, "more"),
    ):
        imported, _ = _call_pull_braintrust(output_path, count=2)

    assert imported == 2
    assert len(output_path.read_text().splitlines()) == 2


def test_pull_braintrust_stops_when_a_page_is_empty(tmp_path):
    output_path = tmp_path / "traces.jsonl"

    with patch(
        "metergraphrelay.providers.braintrust.fetch_spans_page",
        return_value=([], "still-a-cursor"),
    ) as mock_fetch:
        imported, _ = _call_pull_braintrust(output_path)

    assert imported == 0
    assert mock_fetch.call_count == 1


def test_pull_braintrust_follows_the_cursor_across_pages(tmp_path):
    output_path = tmp_path / "traces.jsonl"
    pages = [
        ([make_span(id="span-1")], "next-token"),
        ([make_span(id="span-2")], None),
    ]

    with patch(
        "metergraphrelay.providers.braintrust.fetch_spans_page",
        side_effect=pages,
    ) as mock_fetch:
        imported, _ = _call_pull_braintrust(output_path)

    assert imported == 2
    assert mock_fetch.call_count == 2
    assert "OFFSET" not in mock_fetch.call_args_list[0].kwargs["query"]
    assert "OFFSET 'next-token'" in mock_fetch.call_args_list[1].kwargs["query"]


def test_pull_braintrust_keeps_the_page_limit_stable_across_pages(tmp_path):
    # The cursor is bound to the query that produced it, so the LIMIT must not
    # shrink as rows accumulate.
    output_path = tmp_path / "traces.jsonl"
    pages = [
        ([make_span(id="span-1")], "next-token"),
        ([make_span(id="span-2")], None),
    ]

    with patch(
        "metergraphrelay.providers.braintrust.fetch_spans_page",
        side_effect=pages,
    ) as mock_fetch:
        _call_pull_braintrust(output_path, count=5)

    assert mock_fetch.call_count == 2
    assert all(
        "LIMIT 5" in call.kwargs["query"] for call in mock_fetch.call_args_list
    )


def test_pull_braintrust_caps_the_page_limit_at_the_api_maximum(tmp_path):
    output_path = tmp_path / "traces.jsonl"

    with patch(
        "metergraphrelay.providers.braintrust.fetch_spans_page",
        return_value=([], None),
    ) as mock_fetch:
        _call_pull_braintrust(output_path, count=PAGE_LIMIT * 3)

    assert f"LIMIT {PAGE_LIMIT}" in mock_fetch.call_args.kwargs["query"]


def test_pull_braintrust_aborts_on_a_repeated_cursor(tmp_path):
    output_path = tmp_path / "traces.jsonl"

    with patch(
        "metergraphrelay.providers.braintrust.fetch_spans_page",
        return_value=([make_span()], "same-token"),
    ):
        with pytest.raises(BraintrustAPIError) as exc_info:
            _call_pull_braintrust(output_path, count=100)

    assert "repeated pagination cursor" in str(exc_info.value)


def test_pull_braintrust_skips_a_malformed_span_and_continues(tmp_path, capsys):
    output_path = tmp_path / "traces.jsonl"
    broken = make_span(id="bad-span")
    del broken["created"]

    with patch(
        "metergraphrelay.providers.braintrust.fetch_spans_page",
        return_value=([broken, make_span(id="good-span")], None),
    ):
        imported, skipped = _call_pull_braintrust(output_path)

    assert (imported, skipped) == (1, 1)
    assert "bad-span" in capsys.readouterr().err
    assert len(output_path.read_text().splitlines()) == 1


def test_pull_braintrust_passes_selectors_into_the_query(tmp_path):
    output_path = tmp_path / "traces.jsonl"

    with patch(
        "metergraphrelay.providers.braintrust.fetch_spans_page",
        return_value=([], None),
    ) as mock_fetch:
        _call_pull_braintrust(
            output_path,
            projects=["proj-a", "proj-b"],
            since="2026-08-01T00:00:00+00:00",
        )

    query = mock_fetch.call_args.kwargs["query"]
    assert "project_logs('proj-a', 'proj-b', shape => 'spans')" in query
    assert "AND created >= '2026-08-01T00:00:00+00:00'" in query
    assert "AND created < '2026-09-01T00:00:00+00:00'" in query


def test_pull_braintrust_applies_the_route_override_to_every_row(tmp_path):
    output_path = tmp_path / "traces.jsonl"

    with patch(
        "metergraphrelay.providers.braintrust.fetch_spans_page",
        return_value=([make_span(id="a"), make_span(id="b")], None),
    ):
        _call_pull_braintrust(output_path, route="my-app/bot")

    rows = [json.loads(line) for line in output_path.read_text().splitlines()]
    assert [row["route"] for row in rows] == ["my-app/bot", "my-app/bot"]


def test_pull_braintrust_writes_nothing_when_a_page_request_fails(tmp_path):
    output_path = tmp_path / "traces.jsonl"

    with patch(
        "metergraphrelay.providers.braintrust.fetch_spans_page",
        side_effect=BraintrustAPIError("boom"),
    ):
        with pytest.raises(BraintrustAPIError):
            _call_pull_braintrust(output_path)

    assert not output_path.exists()
    assert list(tmp_path.iterdir()) == []


def test_pull_braintrust_writes_via_temp_file_and_leaves_no_leftover(tmp_path):
    output_path = tmp_path / "traces.jsonl"

    with patch(
        "metergraphrelay.providers.braintrust.fetch_spans_page",
        return_value=([make_span()], None),
    ):
        _call_pull_braintrust(output_path)

    assert [p.name for p in tmp_path.iterdir()] == ["traces.jsonl"]


def test_extract_output_serializes_unrecognized_content_blocks_rather_than_dropping():
    blocks = [{"type": "thinking", "thinking": "internal"}]
    text, calls = _extract_output(blocks)
    assert json.loads(text) == blocks
    assert calls is None


def test_extract_output_serializes_a_message_whose_blocks_are_all_unrecognized():
    message = {"role": "assistant", "content": [{"type": "thinking", "thinking": "x"}]}
    text, calls = _extract_output(message)
    assert json.loads(text) == message
    assert calls is None
