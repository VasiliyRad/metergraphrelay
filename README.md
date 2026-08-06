# metergraphrelay

Try [metergraph](https://www.metergraph.dev/) without installing its SDK:
export the chat completions OpenAI already stores for you, and push them
into metergraph.

1. Your app calls OpenAI with `store=True` — a one-line addition if it
   doesn't already (see below). OpenAI keeps the completion server-side.
2. `metergraphrelay pull openai` lists those stored completions via
   OpenAI's own API and writes them as JSONL, already shaped to
   metergraph's native trace schema.
3. `metergraphrelay push` uploads that file to metergraph.

No SDK, no instrumentation beyond the `store=True` flag — which is also
what OpenAI's own dashboard and evals features use.

This only reads what's associated with the API key you provide; it can't
see or export anyone else's data. Built on OpenAI's
[List Chat Completions API](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/list) —
see the [API reference](https://developers.openai.com/api/reference/chat-completions/overview)
for the underlying data model.

## Setup

    pip install metergraphrelay

Create a `.env` file in your working directory:

    OPENAI_API_KEY=sk-...
    METERGRAPH_APP_TOKEN=...

## Quickstart

    metergraphrelay pull openai -n 25 --output traces.jsonl
    metergraphrelay push traces.jsonl

Or do both in one step:

    metergraphrelay sync openai -n 25 --output traces.jsonl

No stored completions yet? Generate a couple first:

    metergraphrelay demo openai

## Enabling storage on your own calls

`pull openai` only finds completions created with `store=True`. Add it to
calls you're already making:

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "..."}],
        store=True,
        metadata={"source": "my-app"},  # optional, filterable later
    )

Before enabling this in production:
- Stored completions include full request/response content by default.
- There's no automatic expiry from OpenAI's side — delete via their
  [Delete chat completion](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/delete)
  API; `metergraphrelay` doesn't do this for you.

## Commands

    metergraphrelay pull openai -n 25 --output my-traces.jsonl --stdout --include-content --route my-app/support-bot
    metergraphrelay demo openai --model gpt-4o-mini
    metergraphrelay push traces.jsonl
    metergraphrelay sync openai -n 25 --output my-traces.jsonl --route my-app/support-bot

`sync openai` accepts the same flags as `pull openai`. It pulls to
`--output` and immediately pushes that same file, checking both
`OPENAI_API_KEY` and `METERGRAPH_APP_TOKEN` upfront so it fails fast
instead of pulling data it can't push.

`pull anthropic` / `pull langfuse` accept the same shape but aren't
implemented yet — they check for `ANTHROPIC_API_KEY` /
`LANGFUSE_PUBLIC_KEY`+`LANGFUSE_SECRET_KEY` and report accordingly.

All subcommands accept `--env-file PATH`.

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
      "sdk_version": "0.1.2"
    }

`request_json`/`response_text` are populated only when `--include-content`
is passed.

Every completion returned by the stored-completions list already succeeded,
so `status` is always `"success"`. `error`/`error_type` flag a *partial*
record: `--include-content` was requested but the follow-up message fetch
failed, so token counts are still real while the content is missing.

## Development

    git clone https://github.com/VasiliyRad/metergraphrelay
    cd metergraphrelay
    pip install -e ".[dev]"
    pytest
