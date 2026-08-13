# Portkey export sync — design spec

Date: 2026-08-12

## Goal

Add `metergraphrelay sync portkey <export.jsonl>`: convert a Portkey
JSONL log export the user already downloaded into metergraph-native
JSONL, then upload it via the existing `push_file` (`push.py`,
unchanged). No Portkey API call is made anywhere in this feature — the
input is a local file, and no Portkey credential is ever read.

Field mapping below is verified against a real 46,241-row Portkey
export (`gumshoe-portkey-export-3day.jsonl`), inspected locally for key
names and value types only. No customer content is reproduced in this
spec or the repo.

## Scope

In scope: read a local Portkey JSONL export, convert each row, push via
existing `push_file`.

Deferred, not part of this spec: any Portkey API access (`pull
portkey`), CSV/ZIP export formats, hosted-tool replay in the evaluation
pipeline, incremental/resumable uploads, search-result caching.

## CLI

```
metergraphrelay sync portkey EXPORT_FILE [--output converted.jsonl] [--env-file .env]
```

| flag | default | notes |
|---|---|---|
| `EXPORT_FILE` (positional) | required | local Portkey JSONL export |
| `--output` | none — private temp file | see Temp vs. retained output |
| `--env-file` | `.env` | loads only `METERGRAPH_APP_TOKEN`; no Portkey credential exists for this command |

No `--route`: a single export commonly spans many workflows, so route
is derived per row (see Mapping) instead of forced to one value.

`--help`/subcommand description must state plainly: a Portkey
subscription with log export enabled is required; the user downloads
the export from Portkey themselves; this command never contacts
Portkey; request/response content from the export is uploaded to
MeterGraph with no opt-out.

## Data flow

- New module `src/metergraphrelay/providers/portkey.py`, same shape as
  `providers/openai.py`/`providers/langfuse.py`: a pure
  `normalize_portkey_row(row: dict) -> dict`, plus a driving
  `convert_portkey_export(input_path, output_path) -> tuple[int, int]`
  (converted, skipped) that streams the source **line by line** (not
  loaded fully into memory) and writes each converted row immediately.
- `sync portkey` CLI handler: load `METERGRAPH_APP_TOKEN`
  (`require_credentials("push", ...)`) → open `EXPORT_FILE` → convert to
  a temp file → if `converted == 0`, skip the push and report; otherwise
  call existing `push_file` unchanged → print summary → clean up per the
  retention rule below.
- No new runtime dependency (stdlib only, matching `push.py`).

## Temp vs. retained output

Same atomic pattern as `pull langfuse` (`tempfile.mkstemp` + `os.replace`):

- No `--output`: convert to a temp file, push from it, delete it after
  the push attempt — whether the push succeeds or fails.
- `--output PATH`: convert, `os.replace()` onto `PATH` before pushing,
  then retain `PATH` regardless of push outcome (including on push
  failure, so the user can retry with `metergraphrelay push PATH`).
- Zero converted rows: an empty file is still written/retained if
  `--output` was given; no push is attempted either way.

## Verified mapping

Confirmed field names, present across the sampled export (`created_at`,
`ai_org`, `ai_model`, `cost`, `req_units`, `res_units`, `response_time`,
`response_status_code`, `id`, `trace_id`, `request`, `response`,
`metadata.workflow_name`, `metadata.activity_name` on 100% of rows;
`metadata.organization_id`/`report_id`/`organization_action_id`/
`report_run_id`/`answer_id`/`prompt_name` present on most but not all
rows):

| metergraph field | source | notes |
|---|---|---|
| `ts` | `created_at` | already a string timestamp |
| `provider` | `ai_org` | observed values include `openai`, `anthropic`, `vertex-ai`, `perplexity-ai`, `deepseek`, `x-ai` — used as-is, no inference |
| `model` | `ai_model` | |
| `input_tokens` | `req_units` | |
| `output_tokens` | `res_units` | |
| `latency_ms` | `response_time` | |
| `status` / `error` / `error_type` | `response_status_code` | `< 400` → `"success"`/`error=false`; else `"error"`/`error=true`; `error_type` from `response.error` when present (observed on error rows alongside `response.provider`) |
| `cost_usd` | `cost / 100` | export reports cost in cents; this is the only unit conversion this feature performs |
| `request_id`, `span_id` | `id` | same value for both, no separate span identifier exists on the row |
| `trace_id` | `trace_id` | |
| `route` | `metadata.workflow_name`, else `"portkey/backfill"` | present on every sampled row, so the fallback is a defensive default, not the common case |
| `tags` | the entire `metadata` object, copied verbatim | carries `activity_name` plus whichever of `organization_id`/`report_id`/`organization_action_id`/`report_run_id`/`answer_id`/`prompt_name` are present on that row, without naming each as its own mapped field |
| `request_json` | `request`, `json.dumps`'d verbatim | tool declarations preserved exactly as given — observed shapes include OpenAI Responses hosted tools (`type: "web_search"`), Anthropic native tools (`name`/`input_schema`), and chat-completions-style function tools (`type: "function"`, `function.name`) — none reshaped |
| `response_text`, `tool_calls`, `tool_names` | `response`, shape-aware extraction | see Response extraction below |
| `sdk` / `sdk_version` | fixed `"metergraphrelay"` / this tool's `__version__` | unchanged convention |
| `content_opted_in` | fixed `true` | no opt-out for this command |

## Response extraction

Three response shapes were confirmed present in the sample, checked in
this order (first match wins), plus a fallback:

1. **OpenAI Responses** (`response.object == "response"`): iterate
   `response.output`; concatenate `content[].text` from items of type
   `"message"` into `response_text`; every non-`"message"` item (e.g.
   `"web_search_call"`) is appended verbatim to `tool_calls` as hosted
   tool-call evidence — never reshaped into a synthetic function call.
2. **Chat Completions style** (`response.object == "chat.completion"`,
   or `choices` present): `response_text` from
   `choices[0].message.content`; `tool_calls` from
   `choices[0].message.tool_calls` when present.
3. **Anthropic native** (`content` is a list of typed blocks, no
   `object`/`choices` key — observed on `ai_org == "anthropic"` rows):
   concatenate `type: "text"` blocks into `response_text`; `type:
   "tool_use"` blocks appended verbatim to `tool_calls`.
4. **Fallback** (unrecognized shape, or an error response with no
   `output`/`choices`/`content`): `response_text = json.dumps(response)`,
   `tool_calls = null`. Not an error — the row still converts.

`tool_names` is a flat list of `name` values pulled from whatever
`tool_calls` evidence was found; `null` when `tool_calls` is `null`.

## Failure behavior

Fatal (stop before any push, no upload, non-zero exit): missing/invalid
`METERGRAPH_APP_TOKEN`; `EXPORT_FILE` missing/unreadable; `--output`
path unwritable.

Non-fatal, per row (skip, count, continue): invalid JSON on a line; a
row missing a field `normalize_portkey_row` requires. Warning printed
names only the row index and `id` — never row content.

Zero valid rows: no push attempted, exit 0, summary states it plainly.

Always printed at the end: converted / skipped / uploaded / failed
counts, in the same phrasing `sync openai`/`push` already use. A
nonzero `failed` count from `push_file` exits 1, unchanged from today.

Never printed: credentials, or any request/response content from either
the source export or converted rows.

## Testing

One file, `tests/providers/test_portkey.py`, synthetic rows only (no
customer data), mocking `push_file` for CLI-level tests — no network,
no live credentials. Cases:

1. OpenAI Responses with hosted `web_search` — tool declaration and
   `web_search_call` output item preserved verbatim; `response_text`
   pulled from the `message` item only.
2. Vertex-shaped chat-completions with a `type: "function"` tool
   (`google_search`) — extraction via the Chat Completions detector.
3. Anthropic native `content` blocks mixing `text` and `tool_use`.
4. Chat Completions with `message.tool_calls` populated.
5. Malformed-row skipping — bad JSON and a row missing a required
   field are both skipped/counted; warning text asserted to exclude any
   row content.
6. Temp vs. retained output — temp file removed after push (success and
   mocked failure); `--output` file retained in both cases, including
   when conversion yields zero rows.
7. `--help` asserted to contain the Portkey-subscription/no-API-access/
   content-upload statements; CLI-level `sync portkey ... --output`
   integration test with `push_file` mocked, asserting summary counts.

## Non-goals

`pull portkey` / Portkey API access; CSV/ZIP inputs; `--route` override;
content opt-out; hosted-tool replay; incremental/resumable uploads;
search-result caching; per-field mappings for individual optional
metadata IDs (handled by the whole-`metadata`-object passthrough into
`tags` instead).
