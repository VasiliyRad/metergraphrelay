import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from metergraphrelay import __version__
from metergraphrelay.import_identity import ImportContext, ImportIdentityError
from metergraphrelay.providers.phoenix import (
    BACKFILL_ROUTE,
    PAGE_LIMIT,
    PhoenixAPIError,
    _map_input,
    _response_text,
    build_params,
    fetch_spans_page,
    infer_provider,
    normalize_span,
    pull_phoenix,
)


def make_span(**overrides):
    """One LLM span shaped the way GET /v1/projects/{p}/spans returns it."""
    span = {
        "id": "U3Bhbjo2",
        "name": "support-desk/draft-reply",
        "context": {"trace_id": "3ace5c5f331b2bb6", "span_id": "559145fa99b81657"},
        "span_kind": "LLM",
        "parent_id": None,
        "start_time": "2026-09-03T22:23:12.346965+00:00",
        "end_time": "2026-09-03T22:23:13.846965+00:00",
        "status_code": "OK",
        "status_message": "",
        "attributes": {
            "llm.token_count.completion": 28,
            "llm.token_count.prompt": 305,
            "llm.provider": "openai",
            "llm.model_name": "gpt-4o",
            "llm.input_messages.0.message.role": "system",
            "llm.input_messages.0.message.content": "You are a support engineer.",
            "llm.input_messages.1.message.role": "user",
            "llm.input_messages.1.message.content": "Refund the upgrade?",
            "llm.output_messages.0.message.role": "assistant",
            "llm.output_messages.0.message.content": "A refund works here.",
        },
        "events": [],
    }
    span.update(overrides)
    return span


def test_build_params_filters_llm_spans_and_caps_limit():
    params = build_params(since=None, until=None, names=[], limit=25)
    assert params == [("span_kind", "LLM"), ("limit", "25")]


def test_build_params_carries_time_bounds_names_and_cursor():
    params = build_params(
        since="2026-08-01T00:00:00+00:00",
        until="2026-08-07T00:00:00+00:00",
        names=["support-desk/triage", "support-desk/draft-reply"],
        limit=10,
        cursor="U3Bhbjo1",
    )
    assert params == [
        ("span_kind", "LLM"),
        ("limit", "10"),
        ("start_time", "2026-08-01T00:00:00+00:00"),
        ("end_time", "2026-08-07T00:00:00+00:00"),
        ("name", "support-desk/triage"),
        ("name", "support-desk/draft-reply"),
        ("cursor", "U3Bhbjo1"),
    ]


def _mock_response(payload: dict):
    response = MagicMock()
    response.read.return_value = json.dumps(payload).encode()
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    return response


def test_fetch_spans_page_builds_project_url_and_returns_cursor():
    payload = {"data": [make_span()], "next_cursor": "U3Bhbjo1"}
    with patch(
        "metergraphrelay.providers.phoenix.urllib.request.urlopen",
        return_value=_mock_response(payload),
    ) as mock_urlopen:
        rows, cursor = fetch_spans_page(
            "http://localhost:6006/",
            project="my project",
            api_key=None,
            params=build_params(since=None, until=None, names=[], limit=1),
        )

    assert rows == [make_span()]
    assert cursor == "U3Bhbjo1"
    request = mock_urlopen.call_args[0][0]
    assert request.full_url == (
        "http://localhost:6006/v1/projects/my%20project/spans?span_kind=LLM&limit=1"
    )
    assert "Authorization" not in request.headers


def test_fetch_spans_page_sends_bearer_when_api_key_given():
    payload = {"data": [], "next_cursor": None}
    with patch(
        "metergraphrelay.providers.phoenix.urllib.request.urlopen",
        return_value=_mock_response(payload),
    ) as mock_urlopen:
        rows, cursor = fetch_spans_page(
            "http://localhost:6006", project="p", api_key="px-key", params=[]
        )

    assert rows == [] and cursor is None
    request = mock_urlopen.call_args[0][0]
    assert request.get_header("Authorization") == "Bearer px-key"


def test_fetch_spans_page_reports_http_errors_and_hints_on_404():
    error = urllib.error.HTTPError(
        url="http://x", code=404, msg="Not Found", hdrs=None, fp=None
    )
    with patch(
        "metergraphrelay.providers.phoenix.urllib.request.urlopen", side_effect=error
    ):
        with pytest.raises(PhoenixAPIError, match="HTTP 404.*'nope'"):
            fetch_spans_page(
                "http://localhost:6006", project="nope", api_key=None, params=[]
            )


def test_fetch_spans_page_rejects_missing_data():
    with patch(
        "metergraphrelay.providers.phoenix.urllib.request.urlopen",
        return_value=_mock_response({"spans": []}),
    ):
        with pytest.raises(PhoenixAPIError, match="missing/malformed 'data'"):
            fetch_spans_page(
                "http://localhost:6006", project="p", api_key=None, params=[]
            )


def test_fetch_spans_page_rejects_malformed_cursor():
    with patch(
        "metergraphrelay.providers.phoenix.urllib.request.urlopen",
        return_value=_mock_response({"data": [], "next_cursor": 7}),
    ):
        with pytest.raises(PhoenixAPIError, match="malformed pagination cursor"):
            fetch_spans_page(
                "http://localhost:6006", project="p", api_key=None, params=[]
            )


def test_infer_provider_prefers_llm_provider_then_system_then_model_prefix():
    assert infer_provider(make_span()) == "openai"
    span = make_span()
    del span["attributes"]["llm.provider"]
    span["attributes"]["llm.system"] = "Anthropic"
    assert infer_provider(span) == "anthropic"
    del span["attributes"]["llm.system"]
    span["attributes"]["llm.model_name"] = "gemini-2.0-flash"
    assert infer_provider(span) == "google"
    span["attributes"]["llm.model_name"] = "mistral-large"
    assert infer_provider(span) == "unknown"


def test_map_input_rebuilds_flattened_messages():
    request_json, request_text = _map_input(make_span()["attributes"])
    assert request_text is None
    assert json.loads(request_json) == [
        {"role": "system", "content": "You are a support engineer."},
        {"role": "user", "content": "Refund the upgrade?"},
    ]


def test_map_input_falls_back_to_json_input_value_messages():
    attributes = {
        "input.mime_type": "application/json",
        "input.value": json.dumps(
            {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
        ),
    }
    request_json, request_text = _map_input(attributes)
    assert json.loads(request_json) == [{"role": "user", "content": "hi"}]
    assert request_text is None


def test_map_input_keeps_plain_text_input_value_as_text():
    assert _map_input({"input.value": "raw prompt"}) == (None, "raw prompt")
    assert _map_input({}) == (None, None)


def test_response_text_prefers_output_messages_then_output_value():
    assert _response_text(make_span()["attributes"]) == "A refund works here."
    assert _response_text({"output.value": '{"choices": []}'}) == '{"choices": []}'
    assert _response_text({}) is None


def test_normalize_span_maps_a_success_row():
    row = normalize_span(make_span(), project="mgsample", route_override=None)

    assert row["ts"] == "2026-09-03T22:23:12.346965+00:00"
    assert row["source"] == "phoenix"
    assert row["sdk"] == "metergraphrelay"
    assert row["sdk_version"] == __version__
    assert row["provider"] == "openai"
    assert row["model"] == "gpt-4o"
    assert row["status"] == "success"
    assert row["error"] is False and row["error_type"] is None
    assert row["input_tokens"] == 305
    assert row["output_tokens"] == 28
    assert row["cache_read_tokens"] is None
    assert row["latency_ms"] == 1500
    assert row["cost_usd"] is None
    # The span's own name became the route, so it is not repeated in tags.
    assert row["route"] == "support-desk/draft-reply"
    assert row["tags"] == {"phoenix_project": "mgsample"}
    assert row["request_id"] == "559145fa99b81657"
    assert row["span_id"] == "559145fa99b81657"
    assert row["trace_id"] == "3ace5c5f331b2bb6"
    assert row["parent_span_id"] is None
    assert row["content_opted_in"] is True
    assert row["response_text"] == "A refund works here."
    assert row["tool_names"] is None


def test_normalize_span_maps_error_status_and_message():
    span = make_span(status_code="ERROR", status_message="429 rate_limit_exceeded")
    span["attributes"]["llm.token_count.completion"] = 0
    row = normalize_span(span, project="mgsample", route_override=None)

    assert row["status"] == "error"
    assert row["error"] is True
    assert row["error_type"] == "429 rate_limit_exceeded"
    # A failed call still burned prompt tokens; the row must not look free.
    assert row["input_tokens"] == 305
    assert row["output_tokens"] == 0


def test_normalize_span_route_prefers_metergraph_then_genai_then_name():
    span = make_span(name="ChatCompletion")
    span["attributes"]["gen_ai.operation.name"] = "support-desk/triage"
    row = normalize_span(span, project="p", route_override=None)
    assert row["route"] == "support-desk/triage"
    assert row["tags"]["name"] == "ChatCompletion"

    span["attributes"]["metergraph.route"] = "support-desk/explicit"
    row = normalize_span(span, project="p", route_override=None)
    assert row["route"] == "support-desk/explicit"

    # A real OpenInference instrumentor sets neither attribute, so the SDK
    # method name is the best route available.
    plain = make_span(name="ChatCompletion")
    row = normalize_span(plain, project="p", route_override=None)
    assert row["route"] == "ChatCompletion"
    assert "name" not in row["tags"]

    nameless = make_span(name="")
    row = normalize_span(nameless, project="p", route_override=None)
    assert row["route"] == BACKFILL_ROUTE


def test_normalize_span_route_override_preserves_name_in_tags():
    row = normalize_span(make_span(), project="p", route_override="my-app/reply")
    assert row["route"] == "my-app/reply"
    assert row["tags"] == {"phoenix_project": "p", "name": "support-desk/draft-reply"}


def test_normalize_span_reads_token_details_and_tool_names():
    span = make_span(name="ChatCompletion")
    span["attributes"].update(
        {
            "llm.token_count.prompt": 61,
            "llm.token_count.prompt_details.cache_read": 12,
            "llm.token_count.prompt_details.cache_write": 3,
            "llm.token_count.completion_details.reasoning": 5,
            "llm.output_messages.0.message.tool_calls.0.tool_call.function.name": "lookup_invoice",
            "llm.output_messages.0.message.tool_calls.1.tool_call.function.name": "refund",
        }
    )
    row = normalize_span(span, project="p", route_override=None)

    # OpenInference's prompt count already includes cache reads, as does
    # metergraph's, so it is carried across unchanged rather than added to.
    assert row["input_tokens"] == 61
    assert row["cache_read_tokens"] == 12
    assert row["cache_write_tokens"] == 3
    assert row["reasoning_tokens"] == 5
    assert row["tool_names"] == ["lookup_invoice", "refund"]


def test_normalize_span_normalizes_z_designator_and_tolerates_missing_end():
    span = make_span(start_time="2026-08-10T12:00:00Z", end_time=None)
    row = normalize_span(span, project="p", route_override=None)
    assert row["ts"] == "2026-08-10T12:00:00+00:00"
    assert row["latency_ms"] is None


def test_normalize_span_requires_start_time():
    span = make_span()
    del span["start_time"]
    with pytest.raises(KeyError):
        normalize_span(span, project="p", route_override=None)


def _pages(*pages):
    """Side effect for fetch_spans_page: each call returns the next page."""
    iterator = iter(pages)
    return lambda *args, **kwargs: next(iterator)


def test_pull_phoenix_single_page_under_count(tmp_path):
    output = tmp_path / "out.jsonl"
    with patch(
        "metergraphrelay.providers.phoenix.fetch_spans_page",
        side_effect=_pages(([make_span(), make_span(id="b")], None)),
    ):
        imported, skipped = pull_phoenix(
            base_url="http://localhost:6006",
            api_key=None,
            projects=["mgsample"],
            count=10,
            since=None,
            until=None,
            names=[],
            route=None,
            output_path=str(output),
        )

    assert (imported, skipped) == (2, 0)
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert [row["source"] for row in rows] == ["phoenix", "phoenix"]


def test_pull_phoenix_follows_cursor_and_stops_at_count(tmp_path):
    output = tmp_path / "out.jsonl"
    with patch(
        "metergraphrelay.providers.phoenix.fetch_spans_page",
        side_effect=_pages(
            ([make_span(id="a")], "cursor-1"),
            ([make_span(id="b"), make_span(id="c")], "cursor-2"),
            ([make_span(id="d")], None),
        ),
    ) as mock_fetch:
        imported, skipped = pull_phoenix(
            base_url="http://localhost:6006",
            api_key=None,
            projects=["mgsample"],
            count=3,
            since=None,
            until=None,
            names=[],
            route=None,
            output_path=str(output),
        )

    assert (imported, skipped) == (3, 0)
    assert mock_fetch.call_count == 2
    second_params = mock_fetch.call_args_list[1].kwargs["params"]
    assert ("cursor", "cursor-1") in second_params
    # The remaining count caps the page size.
    assert ("limit", "2") in second_params


def test_pull_phoenix_reads_projects_in_order_under_one_cap(tmp_path):
    output = tmp_path / "out.jsonl"
    with patch(
        "metergraphrelay.providers.phoenix.fetch_spans_page",
        side_effect=_pages(([make_span(id="a")], None), ([make_span(id="b")], None)),
    ) as mock_fetch:
        imported, _ = pull_phoenix(
            base_url="http://localhost:6006",
            api_key=None,
            projects=["first", "second"],
            count=10,
            since=None,
            until=None,
            names=[],
            route=None,
            output_path=str(output),
        )

    assert imported == 2
    assert [call.kwargs["project"] for call in mock_fetch.call_args_list] == [
        "first",
        "second",
    ]
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert [row["tags"]["phoenix_project"] for row in rows] == ["first", "second"]


def test_pull_phoenix_caps_page_limit_at_api_maximum(tmp_path):
    with patch(
        "metergraphrelay.providers.phoenix.fetch_spans_page",
        side_effect=_pages(([], None)),
    ) as mock_fetch:
        pull_phoenix(
            base_url="http://localhost:6006",
            api_key=None,
            projects=["p"],
            count=5000,
            since=None,
            until=None,
            names=[],
            route=None,
            output_path=str(tmp_path / "out.jsonl"),
        )
    assert ("limit", str(PAGE_LIMIT)) in mock_fetch.call_args.kwargs["params"]


def test_pull_phoenix_rejects_repeated_cursor(tmp_path):
    output = tmp_path / "out.jsonl"
    with patch(
        "metergraphrelay.providers.phoenix.fetch_spans_page",
        side_effect=_pages(([make_span()], "same"), ([make_span()], "same")),
    ):
        with pytest.raises(PhoenixAPIError, match="repeated pagination cursor"):
            pull_phoenix(
                base_url="http://localhost:6006",
                api_key=None,
                projects=["p"],
                count=10,
                since=None,
                until=None,
                names=[],
                route=None,
                output_path=str(output),
            )
    assert not output.exists()
    assert list(tmp_path.iterdir()) == []


def test_pull_phoenix_skips_malformed_span_and_continues(tmp_path, capsys):
    output = tmp_path / "out.jsonl"
    broken = make_span(id="broken")
    del broken["start_time"]
    with patch(
        "metergraphrelay.providers.phoenix.fetch_spans_page",
        side_effect=_pages(([broken, make_span(id="ok")], None)),
    ):
        imported, skipped = pull_phoenix(
            base_url="http://localhost:6006",
            api_key=None,
            projects=["p"],
            count=10,
            since=None,
            until=None,
            names=[],
            route=None,
            output_path=str(output),
        )

    assert (imported, skipped) == (1, 1)
    assert "skipping malformed span broken" in capsys.readouterr().err


def test_pull_phoenix_requires_a_project(tmp_path):
    with pytest.raises(PhoenixAPIError, match="at least one project"):
        pull_phoenix(
            base_url="http://localhost:6006",
            api_key=None,
            projects=[],
            count=1,
            since=None,
            until=None,
            names=[],
            route=None,
            output_path=str(tmp_path / "out.jsonl"),
        )


def test_messages_fold_content_block_text_into_content():
    attributes = {
        "llm.input_messages.0.message.role": "user",
        "llm.input_messages.0.message.contents.0.message_content.type": "text",
        "llm.input_messages.0.message.contents.0.message_content.text": "Describe this",
        "llm.input_messages.0.message.contents.1.message_content.type": "image",
        "llm.input_messages.0.message.contents.1.message_content.image.image.url": "data:...",
        "llm.input_messages.0.message.contents.2.message_content.type": "text",
        "llm.input_messages.0.message.contents.2.message_content.text": "in detail",
    }
    request_json, request_text = _map_input(attributes)
    assert request_text is None
    assert json.loads(request_json) == [{"role": "user", "content": "Describe this\nin detail"}]


def test_map_input_falls_back_to_input_value_when_every_message_is_blank():
    attributes = {
        "llm.input_messages.0.message.role": "user",
        "llm.input_messages.0.message.unknown.nested": "x",
        "input.mime_type": "application/json",
        "input.value": json.dumps({"messages": [{"role": "user", "content": "the real prompt"}]}),
    }
    request_json, request_text = _map_input(attributes)
    assert json.loads(request_json) == [{"role": "user", "content": "the real prompt"}]
    assert request_text is None
    # With nothing to fall back to, the blank list is still better than nothing.
    attributes.pop("input.value")
    request_json, _ = _map_input(attributes)
    assert json.loads(request_json) == [{"role": "user", "content": ""}]


def test_tool_names_are_read_from_every_output_message():
    attributes = {
        "llm.output_messages.0.message.role": "assistant",
        "llm.output_messages.0.message.tool_calls.0.tool_call.function.name": "lookup",
        "llm.output_messages.1.message.role": "assistant",
        "llm.output_messages.1.message.tool_calls.0.tool_call.function.name": "refund",
        "llm.output_messages.1.message.tool_calls.1.tool_call.function.name": "notify",
    }
    row = normalize_span(make_span(attributes=attributes), project="p", route_override=None)
    assert row["tool_names"] == ["lookup", "refund", "notify"]


def test_latency_survives_a_naive_and_aware_timestamp_pair():
    span = make_span(start_time="2026-09-03T22:23:12+00:00", end_time="2026-09-03T22:23:13")
    row = normalize_span(span, project="p", route_override=None)
    assert row["latency_ms"] is None
    assert row["input_tokens"] == 305  # the span itself still imports


def test_lowercase_utc_designator_is_normalized():
    row = normalize_span(make_span(start_time="2026-08-10T12:00:00z"), project="p", route_override=None)
    assert row["ts"] == "2026-08-10T12:00:00+00:00"


def test_pull_phoenix_filters_non_llm_spans_an_old_server_returns(tmp_path, capsys):
    output = tmp_path / "out.jsonl"
    chain = make_span(id="chain", span_kind="CHAIN", name="pipeline")
    tool = make_span(id="tool", span_kind="TOOL", name="lookup")
    with patch(
        "metergraphrelay.providers.phoenix.fetch_spans_page",
        side_effect=_pages(([chain, make_span(id="llm"), tool], None)),
    ):
        imported, skipped = pull_phoenix(
            base_url="http://localhost:6006",
            api_key=None,
            projects=["p"],
            count=10,
            since=None,
            until=None,
            names=[],
            route=None,
            output_path=str(output),
        )

    assert (imported, skipped) == (1, 0)
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert [row["route"] for row in rows] == ["support-desk/draft-reply"]
    assert "2 non-LLM span(s)" in capsys.readouterr().err


def test_normalize_span_with_import_context_uses_the_otel_span_id_as_identity():
    row = normalize_span(
        make_span(),
        project="mgsample",
        route_override=None,
        import_context=ImportContext(source="phoenix", source_scope="mgsample"),
    )
    assert row["import_source"] == "phoenix"
    assert row["import_source_scope"] == "mgsample"
    assert row["import_event_id"] == "559145fa99b81657" == row["span_id"]
    # Without an OTel context, Phoenix's own span record id is the fallback.
    row = normalize_span(
        make_span(context={}), project="p", route_override=None,
        import_context=ImportContext(source="phoenix", source_scope="p"),
    )
    assert row["import_event_id"] == "U3Bhbjo2"


def test_normalize_span_without_import_context_omits_identity():
    row = normalize_span(make_span(), project="p", route_override=None)
    assert not {"import_source", "import_source_scope", "import_event_id"} & row.keys()


def test_normalize_span_rejects_a_blank_import_event_id():
    with pytest.raises(ImportIdentityError):
        normalize_span(
            make_span(id="", context={"trace_id": "t", "span_id": ""}),
            project="p",
            route_override=None,
            import_context=ImportContext(source="phoenix", source_scope="p"),
        )


def test_pull_phoenix_ticks_progress_per_page_and_per_row_including_skipped(tmp_path):
    broken = make_span(id="broken")
    del broken["start_time"]
    ticks = []
    with patch(
        "metergraphrelay.providers.phoenix.fetch_spans_page",
        side_effect=_pages(([make_span(id="a"), broken], "c1"), ([make_span(id="b")], None)),
    ):
        imported, skipped = pull_phoenix(
            base_url="http://localhost:6006", api_key=None, projects=["p"], count=10,
            since=None, until=None, names=[], route=None,
            output_path=str(tmp_path / "out.jsonl"), on_progress=lambda: ticks.append(1),
        )
    # 2 page fetches + 2 imported + 1 skipped: a window that only pages or
    # only skips still renews the lease.
    assert (imported, skipped) == (2, 1)
    assert len(ticks) == 5
