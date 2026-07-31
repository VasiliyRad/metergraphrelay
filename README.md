# metergraphrelay

Export OpenAI stored chat completions as trace records, and optionally
generate demo conversations to try it out.

## Purpose

Download the chat completions already stored in *your own* OpenAI
account (completions created with `store=True`) as local JSONL trace
records — useful for auditing, backups, or feeding into other tooling.
This tool only reads what's associated with the API key you provide; it
can't see or export anyone else's data. Built to integrate with metergraph,
but also works as a standalone utility.

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
data to export:

    metergraphrelay demo

Export the 10 most recent stored chat completions to `traces.jsonl`:

    metergraphrelay export

Options:

    metergraphrelay export -n 25 --output my-traces.jsonl --stdout
    metergraphrelay demo --model gpt-4o-mini

Both subcommands accept `--env-file PATH` to point at a config file
other than `./.env`.

## Using it against your real system (not just the demo)

`metergraphrelay export` only finds completions that were created with
`store=True`. The `demo` subcommand sets that flag for you, but for your
own application to show up in an export, its own OpenAI calls need the
same flag. The only change required is adding `store=True` (and,
optionally, `metadata` to help you tell requests apart later) to calls
you're already making:

    from openai import OpenAI

    client = OpenAI()

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "..."}],
        store=True,                       # <-- persists this completion for later export
        metadata={"source": "my-app"},    # optional: filter/identify later via the API
    )

No other code changes are needed — the request and response are handled
exactly as before. Once your app is sending `store=True`, its completions
become visible to `metergraphrelay export` (or to the
[List Chat Completions API](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/list)
directly) using the same API key.

Two things worth knowing before flipping this on in production:
- Stored completions include full request/response content by default,
  so treat them as you would any other place your data is retained.
- Storage has no automatic expiry from this tool's side — deletion is a
  separate API call ([Delete chat completion](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/delete)),
  not something `metergraphrelay` currently does.

## Trace record shape

Each line of the export output is a JSON object:

    {
      "id": "chatcmpl-...",
      "ts": "2026-07-30T12:00:00+00:00",
      "model": "gpt-4o-mini",
      "provider": "openai",
      "endpoint": "chat.completions",
      "status": "success",
      "input_tokens": 12,
      "output_tokens": 34,
      "messages": [{"role": "user", "content": "..."}],
      "metadata": {}
    }

## Running tests

    pytest
