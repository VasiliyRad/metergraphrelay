# Portkey Export Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `metergraphrelay sync portkey EXPORT_FILE`, converting a local Portkey JSONL log export into metergraph-native JSONL and uploading it via the existing `push_file`.

**Architecture:** New `src/metergraphrelay/providers/portkey.py` (pure `normalize_portkey_row` + streaming `convert_portkey_export`, no network code), mirroring `providers/openai.py`/`providers/langfuse.py`. `cli.py` gains a `sync portkey` dispatch that loads `METERGRAPH_APP_TOKEN`, converts to a `tempfile.mkstemp` file, then either `os.replace()`s it onto `--output` (retained) or deletes it after the push attempt (no `--output`) — the same mkstemp/`os.replace` pattern `pull_langfuse` already uses. `push_file` is unchanged.

**Tech Stack:** Python 3.10+ stdlib only. No new dependency.

**Spec:** `docs/superpowers/specs/2026-08-12-portkey-export-sync-design.md` — the authority for exact field mappings, response-shape detection order, and failure semantics. This plan states *what* to build and test; consult the spec for the full rationale and verified field list before implementing.

## Global Constraints

- No Portkey API access or Portkey credential anywhere — only `METERGRAPH_APP_TOKEN` via existing `require_credentials("push", ...)`. No `--route` flag (spec: CLI).
- `convert_portkey_export` streams input line by line, never loads the full file.
- No `--output`: temp file deleted after the push attempt, success or failure. `--output PATH`: retained regardless of push outcome, including empty when zero rows convert (spec: Temp vs. retained output).
- Field mapping, response-shape detection order (Responses API → Chat Completions → Anthropic native → JSON-dump fallback), and required fields (`id`, `created_at`, `trace_id`) are exactly as specified — do not re-derive them here, follow the spec (spec: Verified mapping, Response extraction).
- `tool_calls`/`tool_names`/`tags` are native Python objects (list/list/dict) on the row, not pre-stringified — matches `providers/openai.py`/`providers/langfuse.py` and how metergraph's ingest worker itself `json.dumps`s them server-side. `request_json` stays a JSON string, matching existing convention.
- Fatal (no push, non-zero exit): missing/invalid `METERGRAPH_APP_TOKEN`; `EXPORT_FILE` missing/unreadable; `--output` unwritable. Non-fatal per row: invalid JSON, or missing a required field — skip, count, continue. Zero valid rows: no push, exit 0 (spec: Failure behavior).
- Never print credentials or row content — malformed-row warnings name only the line number and, when parseable, the row's `id`.
- `response.error` may be a string or a `{"message": ...}` dict on real error rows; anything else falls back to `json.dumps` — handle all three, don't assume one shape.
- One test file: `tests/providers/test_portkey.py`, synthetic data only, `push_file` mocked for CLI tests.
- No new abstractions (no shape-detector plugin framework, no per-field metadata mappings — the whole-`metadata` passthrough into `tags` covers those), no resumability, no unrelated refactoring.

---

### Task 1: Portkey normalization and streaming conversion

**Files:**
- Create: `src/metergraphrelay/providers/portkey.py`
- Test: `tests/providers/test_portkey.py`

**Interfaces produced (consumed by Task 2):**
```python
def normalize_portkey_row(row: dict) -> dict: ...   # raises KeyError if id/created_at/trace_id missing
def convert_portkey_export(input_path: str, output_path: str) -> tuple[int, int]: ...  # (converted, skipped)
```

- [ ] **Step 1: Write the test file**

Build one synthetic fixture per response shape confirmed in the spec's "Response extraction" section — a Responses-API row with a hosted `web_search` call, a chat-completions-shaped row with a `function`-type tool call (standing in for both the Vertex `google_search` and a plain function-tool case), and an Anthropic-native row with `content` blocks. One representative fixture, to fix the shape other fixtures should follow:

```python
def _responses_row(**overrides):
    row = {
        "id": "pk-req-1", "trace_id": "trace-1", "created_at": "2026-08-10T12:00:00Z",
        "ai_org": "openai", "ai_model": "gpt-5", "cost": 12.5,
        "req_units": 100, "res_units": 40, "response_time": 850,
        "response_status_code": 200,
        "request": {"model": "gpt-5", "input": "...", "tools": [{"type": "web_search"}]},
        "response": {
            "object": "response",
            "output": [
                {"type": "web_search_call", "id": "ws-1", "status": "completed",
                 "action": {"type": "search", "query": "..."}},
                {"type": "message", "id": "msg-1", "role": "assistant",
                 "content": [{"type": "output_text", "text": "Here is the latest on X."}]},
            ],
        },
        "metadata": {"workflow_name": "news-digest", "activity_name": "summarize",
                      "organization_id": "org-42"},
    }
    row.update(overrides)
    return row
```

`_chat_completion_row` and `_anthropic_row` follow the same outer shape (`id`/`trace_id`/`created_at`/`ai_org`/`ai_model`/`cost`/`req_units`/`res_units`/`response_time`/`response_status_code`/`request`/`metadata`), differing only in `response`:

```python
# _chat_completion_row's response — Vertex-routed, function-style google_search tool call
"response": {
    "object": "chat.completion",
    "choices": [{"message": {"role": "assistant", "content": None,
        "tool_calls": [{"id": "call-1", "type": "function",
            "function": {"name": "google_search", "arguments": "{}"}}]},
        "index": 0, "finish_reason": "tool_calls"}],
}

# _anthropic_row's response — native content-block shape
"response": {
    "type": "message", "role": "assistant",
    "content": [
        {"type": "text", "text": "Let me check that for you."},
        {"type": "tool_use", "id": "toolu-1", "name": "get_weather", "input": {"location": "SF"}},
    ],
}
```

Test cases (assert on `normalize_portkey_row(row)`'s return dict unless noted):

- `test_normalize_portkey_row_maps_verified_fields` — full field-by-field check against `_responses_row()`: `ts`, `provider`, `model`, `input_tokens`, `output_tokens`, `latency_ms`, `status="success"`, `error=False`, `cost_usd=0.125` (cents→dollars), `request_id`/`span_id` both `"pk-req-1"`, `trace_id`, `route="news-digest"`, `tags == row["metadata"]`, `sdk`/`sdk_version`/`content_opted_in=True`.
- `test_normalize_portkey_row_route_falls_back_when_workflow_name_missing` — `metadata` without `workflow_name` → `route == "portkey/backfill"`; `tags` still carries the rest of `metadata`.
- `test_normalize_portkey_row_error_status_code_sets_error_fields` — `response_status_code=429`, `response={"error": {"message": "rate limited"}, ...}` → `status="error"`, `error=True`, `error_type="rate limited"`.
- `test_normalize_portkey_row_missing_required_field_raises_key_error` — parametrized over `id`/`created_at`/`trace_id` deleted → `pytest.raises(KeyError)`.
- `test_normalize_portkey_row_openai_responses_hosted_web_search` — `response_text` comes from the `message` item's text only; `tool_calls` contains the `web_search_call` item verbatim (not reshaped); `request_json` round-trips the original `tools` declaration unchanged.
- `test_normalize_portkey_row_vertex_function_style_google_search` — chat-completions row with a `function`-type `google_search` tool call → `tool_names == ["google_search"]`.
- `test_normalize_portkey_row_anthropic_native_tools` — mixed `text`/`tool_use` content blocks → `response_text` is the text blocks only, `tool_calls` contains the `tool_use` block verbatim.
- `test_normalize_portkey_row_chat_completions_with_function_tool_calls` — standard `message.tool_calls` populated → `response_text` from `message.content`, `tool_names` extracted from the call's `function.name`.
- `test_normalize_portkey_row_unrecognized_response_shape_falls_back_to_json_dump` — a response matching none of the three known shapes → `response_text == json.dumps(response)`, `tool_calls is None`.
- `test_convert_portkey_export_streams_and_counts` — two valid rows → `(2, 0)`, two output lines with correct `request_id`s.
- `test_convert_portkey_export_skips_malformed_json_line` — one bad JSON line + one good row → `(1, 1)`; stderr names the line number; asserts the good row's request content is **not** in stderr.
- `test_convert_portkey_export_skips_row_missing_required_field` — one row missing `created_at` + one good row → `(1, 1)`; stderr names the bad row's `id`; no content leak.
- `test_convert_portkey_export_raises_oserror_on_missing_input` — nonexistent input path → `pytest.raises(OSError)`.

- [ ] **Step 2: RED**

Run: `pytest tests/providers/test_portkey.py -v`
Expected: `ModuleNotFoundError: No module named 'metergraphrelay.providers.portkey'`.

- [ ] **Step 3: Implement `providers/portkey.py`**

- `normalize_portkey_row`: pull required fields via `row["id"]`/`row["created_at"]`/`row["trace_id"]` (raises `KeyError` naturally if absent); map the remaining fields per the spec's Verified mapping table; `status`/`error`/`error_type` from `response_status_code` (`>= 400` → error) and `response.get("error")` per the three-shape handling noted in Global Constraints; `cost_usd = cost / 100` when `cost` is numeric else `None`; `route = metadata.get("workflow_name")` or `"portkey/backfill"`; `tags = metadata` verbatim; `request_json = json.dumps(row.get("request"))`.
- A private `_extract_response(response: dict) -> tuple[str | None, list | None]` implementing the four-shape detection order from the spec's "Response extraction" section (Responses API `output` → Chat Completions `choices` → Anthropic `content` blocks → `json.dumps` fallback). Hosted-tool items are appended to `tool_calls` verbatim, never reshaped.
- A private `_tool_names(tool_calls) -> list[str] | None`: best-effort name per call, checking `call["name"]`, then `call["function"]["name"]`, then `call["type"]`.
- `convert_portkey_export`: open both paths, iterate `input_path` line by line; a JSON-decode failure or a `normalize_portkey_row` failure (`KeyError`/`TypeError`/`AttributeError`) increments `skipped` and prints one stderr warning naming only the line number (and row `id` when parseable) — never row content; otherwise write the normalized row and increment `converted`.

- [ ] **Step 4: GREEN**

Run: `pytest tests/providers/test_portkey.py -v`
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/metergraphrelay/providers/portkey.py tests/providers/test_portkey.py
git commit -m "feat(portkey): add normalize_portkey_row and convert_portkey_export"
```

---

### Task 2: `sync portkey` CLI behavior

**Files:**
- Modify: `src/metergraphrelay/cli.py`
- Test: `tests/providers/test_portkey.py` (same file — append a CLI section)

**Interfaces:**
- Consumes Task 1's `normalize_portkey_row`/`convert_portkey_export`; existing `require_credentials`, `ConfigError`, `push_file`, `_config_error`, `_os_error`.
- Produces: `sync portkey EXPORT_FILE [--output PATH] [--env-file PATH]` in `build_parser()`; `_run_sync_portkey(args) -> int`; `_cleanup_temp_file(tmp_path: str) -> None`.

- [ ] **Step 1: Write the CLI tests**

Add fixtures/mocks using `_responses_row` from Task 1 plus `unittest.mock.patch("metergraphrelay.cli.push_file", ...)`. Test cases:

- `test_main_sync_portkey_missing_push_credential_returns_error` — empty `.env` → exit 1, stderr names `METERGRAPH_APP_TOKEN`.
- `test_main_sync_portkey_missing_export_file_returns_clean_error` — nonexistent export path → exit 1, stderr names the path, no traceback.
- `test_main_sync_portkey_prints_converted_skipped_pushed_summary` — one good row + one malformed line, `push_file` mocked to `(1, 0)` → stdout contains `"Converted 1"`, `"skipped 1"`, `"pushed 1"`.
- `test_main_sync_portkey_returns_error_when_push_fails` — `push_file` mocked to `(0, 1)` → exit 1.
- `test_main_sync_portkey_no_output_deletes_temp_file_after_successful_push` / `..._after_failed_push` — no `--output`, `push_file` mocked `(1, 0)` and `(0, 1)` respectively → in both, the path passed to `push_file` no longer exists afterward.
- `test_main_sync_portkey_output_retained_after_failed_push` — `--output PATH`, `push_file` mocked `(0, 1)` → exit 1, `PATH` still exists with the converted row.
- `test_main_sync_portkey_output_retained_and_empty_when_all_rows_malformed` — `--output PATH`, all-malformed input → exit 0, `PATH` exists and is empty, `push_file` never called.
- `test_sync_portkey_help_documents_prerequisites` — `sync portkey --help` output contains: Portkey subscription requirement, log export, "never contacts Portkey", "uploaded to MeterGraph", `--output`, `--env-file`.
- `test_sync_help_lists_portkey_subcommand` — `sync --help` output lists `portkey`.

- [ ] **Step 2: RED**

Run: `pytest tests/providers/test_portkey.py -v -k main_sync_portkey`
Expected: fails — `sync portkey` isn't a registered subcommand yet.

- [ ] **Step 3: Wire it into `cli.py`**

- Add a `sync_portkey_parser` under `sync_subparsers`:

```python
sync_portkey_parser = sync_subparsers.add_parser(
    "portkey", description="...", help="...",  # see prerequisite facts below
)
sync_portkey_parser.add_argument("export_file", metavar="EXPORT_FILE", help="...")
sync_portkey_parser.add_argument("--output", default=None, help="...")
sync_portkey_parser.add_argument("--env-file", default=".env", help="...")
```

  `description=`/`help=` must state the four prerequisite facts from the spec's CLI section (Portkey subscription + log export required, user downloads it themselves, relay never contacts Portkey, content is uploaded to MeterGraph with no opt-out).
- Import `convert_portkey_export`; add `import tempfile`.
- `_cleanup_temp_file(tmp_path)`: `os.remove`, swallow `OSError`.
- `_run_sync_portkey(args)`: load push credentials → create a temp file (in `--output`'s directory via `tempfile.mkstemp` if `--output` given, else a plain `tempfile.mkstemp()`) → `convert_portkey_export` into it (on `OSError`, clean up, return `_os_error`) → if `--output` given, `os.replace()` onto it (on `OSError`, clean up, return `_os_error`); working path is `--output` or the temp path → if `converted == 0`, print the zero-rows summary, clean up temp (only if no `--output`), return 0 without pushing → otherwise call `push_file(working_path, token, base_url=os.environ.get("METERGRAPH_INGEST_URL"))`, clean up temp afterward (only if no `--output`) regardless of push outcome, print the converted/skipped/pushed/failed summary, return `1` if any row failed else `0`.
- Dispatch `if args.command == "sync" and args.provider == "portkey": return _run_sync_portkey(args)` in `main()`.

- [ ] **Step 4: GREEN**

Run: `pytest tests/providers/test_portkey.py -v`
Expected: every test in the file passes.

- [ ] **Step 5: Commit**

```bash
git add src/metergraphrelay/cli.py tests/providers/test_portkey.py
git commit -m "feat(portkey): wire sync portkey into the CLI with temp/retained output handling"
```

---

### Task 3: Documentation and verification

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add a "Sync from Portkey" section**, after "## Pull from Langfuse" and before "## Development", matching that section's existing style (short prose + fenced command examples). Required content, each as its own short paragraph or bullet:
  - This command never contacts Portkey — it only reads a local file.
  - A Portkey subscription with log export enabled is required; the user downloads the export from Portkey themselves.
  - Only `METERGRAPH_APP_TOKEN` is needed — no Portkey credential to configure.
  - Quickstart: `metergraphrelay sync portkey export.jsonl`
  - `--output` retention: `metergraphrelay sync portkey export.jsonl --output converted.jsonl` keeps the converted file even if the upload fails, so `metergraphrelay push converted.jsonl` can retry.
  - Content-upload warning: request/response content from the export is uploaded to MeterGraph, no opt-out (same visibility level as the existing Langfuse warning in that section).
  - Pointer to `metergraphrelay sync portkey --help` for the full flag reference.

- [ ] **Step 2: Add one line to the existing provider-status paragraph** (the one starting `` `pull anthropic` accepts the same shape but isn't implemented yet ``) pointing to the new section, and one example line to the `## Commands` fenced list (`metergraphrelay sync portkey export.jsonl --output converted.jsonl`).

- [ ] **Step 3: Full-suite verification**

Run: `pytest -v`
Expected: every test across `tests/providers/test_portkey.py`, `tests/providers/test_langfuse.py`, `tests/providers/test_openai.py`, `tests/test_cli.py`, `tests/test_config.py`, `tests/test_demo.py`, `tests/test_push.py` passes — zero failures, zero errors.

Run: `python -m metergraphrelay.cli sync portkey --help` and `python -m metergraphrelay.cli sync --help`
Expected: full help text renders the prerequisite statements; subcommand listing shows `openai` and `portkey`.

Run: `git status --short`
Expected: `metergraphrelay.cdx.json` and `requirements.txt` still show as untracked (`??`), unmodified — only `README.md` is new/changed at this point (Tasks 1–2 already committed).

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: add Sync from Portkey section to README"
```
