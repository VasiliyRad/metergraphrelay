# openai-exporter

Export OpenAI stored chat completions as trace records, and optionally
generate demo conversations to try it out.

## Setup

    pip install -e ".[dev]"
    cp .env.example .env
    # edit .env and set OPENAI_API_KEY

## Usage

Generate 1-2 demo conversations (stored with `store=True`) so there's
data to export:

    openai-exporter demo

Export the 10 most recent stored chat completions to `traces.jsonl`:

    openai-exporter export

Options:

    openai-exporter export -n 25 --output my-traces.jsonl --stdout
    openai-exporter demo --model gpt-4o-mini

Both subcommands accept `--env-file PATH` to point at a config file
other than `./.env`.

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
