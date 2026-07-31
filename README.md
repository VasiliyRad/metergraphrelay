# metergraphrelay

Export OpenAI stored chat completions as trace records, and optionally
generate demo conversations to try it out.

## Purpose

Download the chat completions already stored in *your own* OpenAI
account (completions created with `store=True`) as local JSONL trace
records — useful for auditing, backups, or feeding into other tooling.
This tool only reads what's associated with the API key you provide; it
can't see or export anyone else's data.

Built on OpenAI's [List Chat Completions API](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/list),
which returns completions that were created with `store: true`. See the
[official OpenAI API reference](https://developers.openai.com/api/reference/chat-completions/overview)
for the underlying data model.

## Setup

    pip install -e ".[dev]"
    cp .env.example .env
    # edit .env and set OPENAI_API_KEY

## Usage

Generate 1-2 demo conversations (stored with `store=True`) so there's
data to pull:

    metergraphrelay demo openai

Pull the 10 most recent stored chat completions into `traces.jsonl`,
already shaped as metergraph trace records:

    metergraphrelay pull openai

Options:

    metergraphrelay pull openai -n 25 --output my-traces.jsonl --stdout --include-content --route my-app/support-bot
    metergraphrelay demo openai --model gpt-4o-mini

Push a local JSONL file of traces to metergraph:

    metergraphrelay push traces.jsonl

`pull anthropic` and `pull langfuse` accept the same shape but are not
yet implemented in this version — they check for `ANTHROPIC_API_KEY` /
`LANGFUSE_PUBLIC_KEY`+`LANGFUSE_SECRET_KEY` and report accordingly.

All subcommands accept `--env-file PATH` to point at a config file
other than `./.env`.

## Using it against your real system (not just the demo)

`metergraphrelay pull openai` only finds completions that were created
with `store=True`. The `demo openai` subcommand sets that flag for you,
but for your own application to show up in a pull, its own OpenAI calls
need the same flag. The only change required is adding `store=True`
(and, optionally, `metadata` to help you tell requests apart later) to
calls you're already making:

    from openai import OpenAI

    client = OpenAI()

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "..."}],
        store=True,                       # <-- persists this completion for later pulling
        metadata={"source": "my-app"},    # optional: filter/identify later via the API
    )

No other code changes are needed — the request and response are handled
exactly as before. Once your app is sending `store=True`, its completions
become visible to `metergraphrelay pull openai` (or to the
[List Chat Completions API](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/list)
directly) using the same API key.

Two things worth knowing before flipping this on in production:
- Stored completions include full request/response content by default,
  so treat them as you would any other place your data is retained.
- Storage has no automatic expiry from this tool's side — deletion is a
  separate API call ([Delete chat completion](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/delete)),
  not something `metergraphrelay` currently does.

## Trace record shape

Each line of `pull openai`'s output is a JSON object already shaped for
metergraph's ingest API:

    {
      "ts": "2026-07-30T12:00:00+00:00",
      "provider": "openai",
      "model": "gpt-4o-mini",
      "status": "success",
      "endpoint": "chat.completions",
      "input_tokens": 12,
      "output_tokens": 34,
      "error": false,
      "error_type": null,
      "request_id": "chatcmpl-...",
      "tags": {},
      "route": "openai/backfill",
      "content_opted_in": false,
      "request_json": null,
      "response_text": null,
      "sdk": "metergraphrelay",
      "sdk_version": "0.1.0"
    }

`request_json`/`response_text` are populated only when `--include-content`
is passed.

Every completion returned by the stored-completions list already succeeded, so
`status` is always `"success"`. `error`/`error_type` flag a *partial* record:
`--include-content` was requested but the follow-up message fetch failed, so
token counts are still real while the content is missing.

## Running tests

    pytest
