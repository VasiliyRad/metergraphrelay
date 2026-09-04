import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from metergraphrelay import __version__
from metergraphrelay.import_identity import ImportContext, ImportIdentityError
from metergraphrelay.providers.langsmith import (
    BACKFILL_ROUTE,
    PAGE_LIMIT,
    SELECT_FIELDS,
    LangSmithAPIError,
    _map_input,
    _response_text,
    build_filter,
    build_query,
    fetch_runs_page,
    infer_provider,
    map_usage,
    normalize_run,
    pull_langsmith,
    resolve_project_ids,
)

PROJECT_ID = "5f6c1a2e-3b4d-4e5f-8a9b-0c1d2e3f4a5b"


def make_run(**overrides):
    """One LLM run shaped the way POST /runs/query returns it."""
    run = {
        "id": "0192b7c1-1111-7000-8000-000000000001",
        "name": "support-desk/triage",
        "run_type": "llm",
        "start_time": "2026-09-04T01:37:10.123456",
        "end_time": "2026-09-04T01:37:11.623456",
        "error": None,
        "inputs": {"messages": [{"role": "system", "content": "Triage."},
                                {"role": "user", "content": "Invoice 88213 double charged"}]},
        "outputs": {"role": "assistant", "content": "billing; P1"},
        "extra": {"metadata": {"ls_model_name": "gpt-4o-mini", "ls_provider": "openai",
                               "usage_metadata": {"input_tokens": 419, "output_tokens": 87,
                                                  "total_tokens": 506}}},
        "tags": ["prod", "retest"],
        "session_id": PROJECT_ID,
        "trace_id": "0192b7c1-0000-7000-8000-00000000000a",
        "parent_run_id": "0192b7c1-0000-7000-8000-00000000000b",
        "prompt_tokens": 419,
        "completion_tokens": 87,
        "total_tokens": 506,
        "prompt_token_details": {"cache_read": 12},
        "completion_token_details": {"reasoning": 5},
        "total_cost": 0.000115,
    }
    run.update(overrides)
    return run


def test_build_filter_combines_until_names_and_tags():
    assert build_filter(until=None, names=[], tags=[]) is None
    assert build_filter(until="2026-09-04T02:00:00+00:00", names=[], tags=[]) == \
        'lt(start_time, "2026-09-04T02:00:00+00:00")'
    assert build_filter(until=None, names=["a"], tags=[]) == 'eq(name, "a")'
    assert build_filter(until=None, names=["a", "b"], tags=[]) == 'or(eq(name, "a"), eq(name, "b"))'
    assert build_filter(until="U", names=["a"], tags=["prod", "t1"]) == \
        'and(lt(start_time, "U"), eq(name, "a"), has(tags, "prod"), has(tags, "t1"))'
    # Quotes inside a value cannot break out of the expression.
    assert build_filter(until=None, names=['x"y'], tags=[]) == 'eq(name, "x\\"y")'


def test_build_query_targets_llm_runs_and_carries_selectors_and_cursor():
    body = build_query(
        project_ids=[PROJECT_ID], since="S", until="U", names=["n"], tags=["t"],
        limit=25, cursor="c1",
    )
    assert body["session"] == [PROJECT_ID]
    assert body["run_type"] == "llm"
    assert body["start_time"] == "S"
    assert body["filter"] == 'and(lt(start_time, "U"), eq(name, "n"), has(tags, "t"))'
    assert body["limit"] == 25
    assert body["cursor"] == "c1"
    assert set(SELECT_FIELDS) <= set(body["select"])
    minimal = build_query(project_ids=[PROJECT_ID], since=None, until=None, names=[], tags=[], limit=5)
    assert "start_time" not in minimal and "filter" not in minimal and "cursor" not in minimal


def _mock_response(payload):
    response = MagicMock()
    response.read.return_value = json.dumps(payload).encode()
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    return response


def test_resolve_project_ids_passes_uuids_and_looks_names_up():
    with patch(
        "metergraphrelay.providers.langsmith.urllib.request.urlopen",
        return_value=_mock_response([{"id": PROJECT_ID, "name": "mgsample"}]),
    ) as mock_urlopen:
        ids = resolve_project_ids("https://api.smith.langchain.com", api_key="k",
                                  projects=[PROJECT_ID, "mgsample"])
    assert ids == [PROJECT_ID, PROJECT_ID]
    request = mock_urlopen.call_args[0][0]
    assert request.full_url == "https://api.smith.langchain.com/sessions?name=mgsample&limit=1"
    assert request.get_header("X-api-key") == "k"


def test_resolve_project_ids_rejects_an_unknown_name():
    with patch(
        "metergraphrelay.providers.langsmith.urllib.request.urlopen",
        return_value=_mock_response([{"id": PROJECT_ID, "name": "other"}]),
    ):
        with pytest.raises(LangSmithAPIError, match="'nope' not found"):
            resolve_project_ids("https://api.smith.langchain.com", api_key="k", projects=["nope"])


def test_fetch_runs_page_posts_the_body_and_returns_the_cursor():
    payload = {"runs": [make_run()], "cursors": {"next": "abc"}}
    with patch(
        "metergraphrelay.providers.langsmith.urllib.request.urlopen",
        return_value=_mock_response(payload),
    ) as mock_urlopen:
        runs, cursor = fetch_runs_page("https://api.smith.langchain.com/", api_key="k",
                                       body={"session": [PROJECT_ID]})
    assert runs == [make_run()] and cursor == "abc"
    request = mock_urlopen.call_args[0][0]
    assert request.full_url == "https://api.smith.langchain.com/runs/query"
    assert request.get_method() == "POST"
    assert json.loads(request.data) == {"session": [PROJECT_ID]}


@pytest.mark.parametrize("payload,message", [
    ({"items": []}, "missing/malformed 'runs'"),
    ({"runs": [], "cursors": {"next": 7}}, "malformed pagination cursor"),
])
def test_fetch_runs_page_rejects_bad_bodies(payload, message):
    with patch(
        "metergraphrelay.providers.langsmith.urllib.request.urlopen",
        return_value=_mock_response(payload),
    ):
        with pytest.raises(LangSmithAPIError, match=message):
            fetch_runs_page("https://api.smith.langchain.com", api_key="k", body={})


def test_fetch_runs_page_wraps_http_and_network_errors():
    error = urllib.error.HTTPError(url="x", code=401, msg="Unauthorized", hdrs=None, fp=None)
    with patch("metergraphrelay.providers.langsmith.urllib.request.urlopen", side_effect=error):
        with pytest.raises(LangSmithAPIError, match="HTTP 401"):
            fetch_runs_page("https://api.smith.langchain.com", api_key="k", body={})
    with patch("metergraphrelay.providers.langsmith.urllib.request.urlopen",
               side_effect=urllib.error.URLError("refused")):
        with pytest.raises(LangSmithAPIError, match="refused"):
            fetch_runs_page("https://api.smith.langchain.com", api_key="k", body={})


def test_infer_provider_prefers_ls_provider_then_model_prefix():
    assert infer_provider(make_run()) == "openai"
    run = make_run(extra={"metadata": {"ls_model_name": "claude-3-5-haiku-latest"}})
    assert infer_provider(run) == "anthropic"
    assert infer_provider(make_run(extra={"invocation_params": {"model": "mistral-large"}})) == "unknown"


def test_map_usage_prefers_rolled_up_counts_and_falls_back_to_usage_metadata():
    usage = map_usage(make_run())
    assert usage == {"input_tokens": 419, "output_tokens": 87, "cache_read_tokens": 12,
                     "cache_write_tokens": None, "reasoning_tokens": 5}
    raw_only = make_run(prompt_tokens=None, completion_tokens=None, prompt_token_details=None,
                        completion_token_details=None,
                        extra={"metadata": {"usage_metadata": {
                            "input_tokens": 30, "output_tokens": 4,
                            "input_token_details": {"cache_read": 10, "cache_creation": 3},
                            "output_token_details": {"reasoning": 2}}}})
    assert map_usage(raw_only) == {"input_tokens": 30, "output_tokens": 4, "cache_read_tokens": 10,
                                   "cache_write_tokens": 3, "reasoning_tokens": 2}
    assert map_usage(make_run(prompt_tokens=True, completion_tokens=None, prompt_token_details=None,
                              completion_token_details=None, extra={})) == {
        "input_tokens": None, "output_tokens": None, "cache_read_tokens": None,
        "cache_write_tokens": None, "reasoning_tokens": None}


def test_map_input_handles_plain_langchain_and_prompt_shapes():
    plain = {"messages": [{"role": "user", "content": "hi"}]}
    assert _map_input(plain) == (json.dumps(plain["messages"]), None)
    langchain = {"messages": [[{"lc": 1, "id": ["langchain", "schema", "HumanMessage"],
                                "kwargs": {"content": "hi", "type": "human"}}]]}
    assert json.loads(_map_input(langchain)[0]) == [{"role": "user", "content": "hi"}]
    assert _map_input({"prompt": "raw"}) == (None, "raw")
    assert _map_input({"other": 1}) == (None, '{"other": 1}')
    assert _map_input(None) == (None, None)


def test_response_text_handles_the_shapes_langsmith_records():
    assert _response_text({"generations": [[{"text": "gen"}]]}) == "gen"
    assert _response_text({"generations": [[{"message": {"kwargs": {"content": "msg"}}}]]}) == "msg"
    assert _response_text({"choices": [{"message": {"content": "choice"}}]}) == "choice"
    assert _response_text({"role": "assistant", "content": "bare"}) == "bare"
    assert _response_text({"output": {"content": "nested"}}) == "nested"
    assert _response_text({"answer": 1}) == '{"answer": 1}'
    assert _response_text(None) is None


def test_normalize_run_maps_a_success_row():
    row = normalize_run(make_run(), route_override=None)
    # LangSmith returns naive UTC timestamps; the row makes the offset explicit.
    assert row["ts"] == "2026-09-04T01:37:10.123456+00:00"
    assert row["source"] == "langsmith"
    assert row["sdk_version"] == __version__
    assert row["provider"] == "openai" and row["model"] == "gpt-4o-mini"
    assert row["status"] == "success" and row["error"] is False and row["error_type"] is None
    assert (row["input_tokens"], row["output_tokens"]) == (419, 87)
    assert row["cache_read_tokens"] == 12 and row["reasoning_tokens"] == 5
    assert row["latency_ms"] == 1500
    assert row["cost_usd"] == 0.000115
    assert row["route"] == "support-desk/triage"
    assert row["tags"] == {"langsmith_tags": ["prod", "retest"], "langsmith_project_id": PROJECT_ID}
    assert row["request_id"] == row["span_id"] == "0192b7c1-1111-7000-8000-000000000001"
    assert row["trace_id"].endswith("0a") and row["parent_span_id"].endswith("0b")
    assert json.loads(row["request_json"])[1]["content"] == "Invoice 88213 double charged"
    assert row["response_text"] == "billing; P1"
    assert "import_source" not in row


def test_normalize_run_maps_errors_and_route_fallbacks():
    row = normalize_run(make_run(error="429 rate_limit_exceeded", completion_tokens=0),
                        route_override=None)
    assert row["status"] == "error" and row["error_type"] == "429 rate_limit_exceeded"
    assert row["input_tokens"] == 419  # a failed call still burned its prompt
    row = normalize_run(make_run(name="ChatOpenAI"), route_override="support-desk/triage")
    assert row["route"] == "support-desk/triage" and row["tags"]["name"] == "ChatOpenAI"
    row = normalize_run(make_run(name=""), route_override=None)
    assert row["route"] == BACKFILL_ROUTE


def test_normalize_run_with_import_context_uses_the_run_id_as_identity():
    row = normalize_run(make_run(), route_override=None,
                        import_context=ImportContext(source="langsmith", source_scope="mgsample"))
    assert row["import_source"] == "langsmith"
    assert row["import_source_scope"] == "mgsample"
    assert row["import_event_id"] == row["request_id"]
    with pytest.raises(ImportIdentityError):
        normalize_run(make_run(id=""), route_override=None,
                      import_context=ImportContext(source="langsmith", source_scope="s"))


def test_normalize_run_requires_a_start_time():
    with pytest.raises(KeyError):
        normalize_run(make_run(start_time=None), route_override=None)


def _pages(*pages):
    iterator = iter(pages)
    return lambda *args, **kwargs: next(iterator)


def _pull(tmp_path, **overrides):
    kwargs = dict(base_url="https://api.smith.langchain.com", api_key="k", projects=[PROJECT_ID],
                  count=10, since=None, until=None, names=[], tags=[], route=None,
                  output_path=str(tmp_path / "out.jsonl"))
    kwargs.update(overrides)
    return pull_langsmith(**kwargs)


def test_pull_langsmith_follows_cursor_stops_at_count_and_ticks(tmp_path):
    ticks = []
    broken = make_run(id="broken", start_time=None)
    with patch(
        "metergraphrelay.providers.langsmith.fetch_runs_page",
        side_effect=_pages(([make_run(id="a"), broken], "c1"), ([make_run(id="b"), make_run(id="c")], None)),
    ) as mock_fetch:
        imported, skipped = _pull(tmp_path, count=2, on_progress=lambda: ticks.append(1))
    assert (imported, skipped) == (2, 1)
    assert mock_fetch.call_count == 2
    assert mock_fetch.call_args_list[1].kwargs["body"]["cursor"] == "c1"
    assert mock_fetch.call_args_list[1].kwargs["body"]["limit"] == 1
    # project resolution + 2 pages + 2 imported + 1 skipped
    assert len(ticks) == 6
    rows = [json.loads(line) for line in (tmp_path / "out.jsonl").read_text().splitlines()]
    assert [r["request_id"] for r in rows] == ["a", "b"]


def test_pull_langsmith_resolves_project_names_before_querying(tmp_path):
    with patch(
        "metergraphrelay.providers.langsmith.resolve_project_ids", return_value=[PROJECT_ID]
    ) as resolve, patch(
        "metergraphrelay.providers.langsmith.fetch_runs_page", side_effect=_pages(([], None))
    ) as fetch:
        _pull(tmp_path, projects=["mgsample"], since="S", until="U", names=["n"], tags=["t"])
    resolve.assert_called_once()
    body = fetch.call_args.kwargs["body"]
    assert body["session"] == [PROJECT_ID]
    assert body["start_time"] == "S" and 'lt(start_time, "U")' in body["filter"]
    assert body["limit"] == 10


def test_pull_langsmith_caps_page_limit_and_rejects_repeated_cursor(tmp_path):
    with patch("metergraphrelay.providers.langsmith.fetch_runs_page", side_effect=_pages(([], None))) as fetch:
        _pull(tmp_path, count=5000)
    assert fetch.call_args.kwargs["body"]["limit"] == PAGE_LIMIT
    scratch = tmp_path / "loop"
    scratch.mkdir()
    with patch("metergraphrelay.providers.langsmith.fetch_runs_page",
               side_effect=_pages(([make_run()], "same"), ([make_run()], "same"))):
        with pytest.raises(LangSmithAPIError, match="repeated pagination cursor"):
            _pull(scratch, output_path=str(scratch / "out.jsonl"))
    # No output and no leftover temp file after the abort.
    assert list(scratch.iterdir()) == []


def test_pull_langsmith_requires_a_project(tmp_path):
    with pytest.raises(LangSmithAPIError, match="at least one project"):
        _pull(tmp_path, projects=[])
