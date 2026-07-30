# openai-exporter: design spec

Date: 2026-07-30

## Purpose

A Python command-line tool that exports N traces (OpenAI stored chat
completions) from an OpenAI account, and can optionally run a couple of
demo conversations first so there's something to export.

## Background / data source

OpenAI has no generic "list all past traces" API. The closest real
equivalent is **Stored Chat Completions**: a chat completion created with
`store=True` is persisted server-side and can later be listed
(`GET /v1/chat/completions`) and its messages retrieved
(`GET /v1/chat/completions/{id}/messages`).

This mirrors (a subset of) the trace-row concept used by the
`metergraphsdk` project (see `python/src/metergraph/_capture.py`), which
builds a normalized record per LLM call (timestamp, model, token usage,
request/response content, status, etc.) by wrapping the client at
call-time. `openai-exporter` instead pulls already-stored completions
after the fact — it does not intercept live calls except in the `demo`
subcommand, where it makes the calls itself.

Only fields OpenAI's stored-completion API can actually populate are
included in the exported trace row. Fields metergraph captures that have
no OpenAI equivalent (latency_ms, ttft_ms, detailed tool-call events,
source stack frames, session/route tags) are omitted rather than
fabricated.

## CLI

Two subcommands:

### `openai-exporter demo`

Runs 1-2 fixed, harmless prompts against the OpenAI API with
`store=True`, using a small default model. Guarantees the account has at
least one stored completion to export, so it can be used to demonstrate
that `export` is working end-to-end.

Flags:
- `--model` (default: `gpt-4o-mini`)
- `--env-file` (default: `.env` in cwd)

Behavior: prints each prompt and the model's reply to stdout as it runs.
No file output.

### `openai-exporter export`

Lists the N most recent stored chat completions
(`chat.completions.list(order="desc", limit=N)`), fetches full messages
for each one (`chat.completions.messages.list(completion_id)`),
normalizes each into a trace row, and writes them as JSON Lines.

Flags:
- `-n / --count` (default: `10`) — number of traces to export
- `--output` (default: `./traces.jsonl`) — output file path
- `--stdout` — also print each row to stdout as it's written
- `--env-file` (default: `.env` in cwd)

Export scope: all stored completions visible to the API key, most recent
first — not filtered to completions created by this tool's `demo`
command. This matches "export N traces from OpenAI" literally and keeps
the tool simple.

## Config

A `.env` file (default path: `.env` in the current working directory,
overridable with `--env-file` on either subcommand), loaded via
`python-dotenv`:

```
OPENAI_API_KEY=sk-...
```

No other settings live in the config file. Everything else (`-n`,
`--output`, `--model`) is a CLI flag with a sensible default, so the
config file's only job is holding the key.

A missing or empty `OPENAI_API_KEY` is a hard error raised before any
network call, with a clear message pointing at `.env.example`.

## Trace row shape

One JSON object per line in the output file:

```json
{
  "id": "chatcmpl-...",
  "ts": "2026-07-30T12:00:00+00:00",
  "model": "gpt-4o-mini",
  "provider": "openai",
  "endpoint": "chat.completions",
  "status": "success",
  "input_tokens": 12,
  "output_tokens": 34,
  "messages": [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ],
  "metadata": {}
}
```

- `id`: the completion's id (`chatcmpl-...`)
- `ts`: ISO 8601 UTC, derived from the completion's `created` unix
  timestamp
- `model`, `metadata`: taken directly from the list response
- `input_tokens` / `output_tokens`: from the completion's `usage` object,
  when present; `null` if absent
- `status`: `"success"` unless the SDK/API reports an error for that item,
  in which case `"error"`
- `messages`: full list from `chat.completions.messages.list`, each as
  `{"role": ..., "content": ...}`

## Error handling

- Missing/invalid API key: fail fast with a clear message, before any API
  call.
- Empty stored-completions list: `export` writes an empty (zero-line)
  output file and prints a note suggesting `openai-exporter demo` first.
- OpenAI API errors (auth, rate limit, etc.): let the SDK's exception
  message surface directly to the user; no retry logic. This is a demo/
  utility tool, not a production data pipeline.

## Project layout

```
openai-exporter/
  pyproject.toml
  README.md
  .env.example
  src/openai_exporter/
    __init__.py
    cli.py        # argparse: top-level parser + demo/export subcommands
    config.py      # .env loading, OPENAI_API_KEY resolution
    demo.py        # runs the 1-2 canned conversations
    export.py      # list + fetch + normalize + write jsonl
  tests/
    test_export.py   # normalization logic, mocked OpenAI client
    test_demo.py      # demo flow, mocked OpenAI client
```

- Packaging: `pyproject.toml` with a `console_scripts` entry point
  (`openai-exporter = openai_exporter.cli:main`), so `pip install .`
  puts `openai-exporter` on PATH.
- Python 3.10+.
- Dependencies: `openai`, `python-dotenv`. No CLI framework beyond stdlib
  `argparse`.
- Tests mock the OpenAI client (`unittest.mock`) — no real API calls in
  the test suite.

## Out of scope

- Filtering export to only this tool's own demo completions (metadata
  tagging/filtering).
- Any trace source other than Stored Chat Completions (e.g. Assistants/
  Threads runs, Responses API, Evals).
- Retry/backoff, pagination beyond a single `limit=N` list call,
  rate-limit handling.
- Non-JSONL output formats (CSV, JSON array).
