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
    metergraphrelay sync portkey export.jsonl --output converted.jsonl

`sync openai` accepts the same flags as `pull openai`. It pulls to
`--output` and immediately pushes that same file, checking both
`OPENAI_API_KEY` and `METERGRAPH_APP_TOKEN` upfront so it fails fast
instead of pulling data it can't push.

`pull anthropic` accepts the same shape but isn't implemented yet — it
checks for `ANTHROPIC_API_KEY` and reports accordingly. `pull langfuse`
is implemented — see [Pull from Langfuse](#pull-from-langfuse) below.
`sync portkey` runs in two modes: give it a local `EXPORT_FILE` to
convert a Portkey export you downloaded yourself
([Sync from Portkey](#sync-from-portkey)), or omit the file to pull a
window directly from the Portkey Logs Export API on a cron
([Sync from Portkey (API cron mode)](#sync-from-portkey-api-cron-mode)).

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
      "sdk_version": "0.1.3"
    }

`request_json`/`response_text` are populated only when `--include-content`
is passed.

Every completion returned by the stored-completions list already succeeded,
so `status` is always `"success"`. `error`/`error_type` flag a *partial*
record: `--include-content` was requested but the follow-up message fetch
failed, so token counts are still real while the content is missing.

## Pull from Langfuse

Import Langfuse **GENERATION** observations (the LLM call records
Langfuse captures) into the same metergraph-native JSONL shape as
`pull openai`. Only `GENERATION` observations are imported — Langfuse
`SPAN`/`EVENT` observations and Scores/evals are never imported.
Requires Langfuse Cloud or **self-hosted v4+** (the version serving the
v2 Observations API); older self-hosted deployments are not supported.

**Setup:** add your Langfuse keys to `.env`:

    LANGFUSE_PUBLIC_KEY=pk-lf-...
    LANGFUSE_SECRET_KEY=sk-lf-...

By default this talks to Langfuse Cloud. For a self-hosted instance,
set `LANGFUSE_BASE_URL` in `.env` (or pass `--base-url` per-command):

    LANGFUSE_BASE_URL=https://your-langfuse-instance.example.com

**Quickstart:**

    metergraphrelay pull langfuse -n 25 --output traces.jsonl
    metergraphrelay push traces.jsonl

With no other flags, this imports the latest 100 `GENERATION`
observations overall (not 100 distinct traces).

**Narrowing what gets pulled**, beyond `-n`/`--count`:

    metergraphrelay pull langfuse --since 2026-08-01T00:00:00Z --until 2026-08-07T00:00:00Z
    metergraphrelay pull langfuse --trace-name support-bot-reply --trace-name billing-bot-reply --tag prod --tag tier-1

- `--trace-name` matches Langfuse's trace name — the closest Langfuse
  concept to a workflow or use case (e.g. `"support-bot-reply"`). It's
  repeatable; multiple `--trace-name` values are **OR'd** together (any
  match).
- `--tag` matches Langfuse trace tags — commonly used as customer-defined
  categories (a tenant, an experiment cohort, a priority tier); this is
  a convention, not something Langfuse enforces. It's repeatable;
  multiple `--tag` values require **all** of them to be present (AND).
  `--tag` only matches tags that already exist on your historical
  data — it can't require a tag that was never set, and if you don't
  pass `--tag` at all, there's no tag-based narrowing (not "untagged
  only").
- `--trace-name`, `--tag`, `--environment`, and `--since`/`--until` all
  combine with each other using AND.
- `--count` is always a cap on the number of **GENERATION observations**
  imported, never a count of distinct traces.

**Before running this against your own data:** `pull langfuse`
transfers every matched generation's prompt/response content from
Langfuse into your local JSONL file, and from there into metergraph via
`push`, with no separate opt-in step — unlike `pull openai`'s
`--include-content` flag, there is no way to pull Langfuse generations
without their content.

Full flag reference: `metergraphrelay pull langfuse --help`.

## Sync from Portkey

Convert a Portkey JSONL log export you've already downloaded into
metergraph-native JSONL and upload it in one step. In this manual mode
the command never contacts Portkey — it only reads a local file. To
pull from the Portkey API instead, omit the file — see
[Sync from Portkey (API cron mode)](#sync-from-portkey-api-cron-mode).

Requires a Portkey subscription with log export enabled. Download the
export from Portkey yourself first; `metergraphrelay` doesn't fetch it
for you and never sees your Portkey account.

**Quickstart:**

    metergraphrelay sync portkey export.jsonl

Only `METERGRAPH_APP_TOKEN` is needed — there's no Portkey credential to
configure.

To keep the converted metergraph-native file instead of a private
temporary one that's deleted after upload:

    metergraphrelay sync portkey export.jsonl --output converted.jsonl

`--output` is retained even if the upload fails, so you can retry with
`metergraphrelay push converted.jsonl`.

**Before running this against your own export:** request and response
content from the export is uploaded to MeterGraph, with no opt-out.

Full flag reference: `metergraphrelay sync portkey --help`.

## Sync from Portkey (API cron mode)

Run `sync portkey` with **no `EXPORT_FILE`** and it pulls a window of
logs directly from the Portkey **Logs Export API** (unlike manual mode,
this contacts Portkey) and pushes them to metergraph in one step. It is
designed to run unattended from cron.

**Setup:** in addition to `METERGRAPH_APP_TOKEN`, this mode requires
`PORTKEY_API_KEY` (a secret) in `.env`:

    METERGRAPH_APP_TOKEN=your-metergraph-token-here
    PORTKEY_API_KEY=pk-your-portkey-key-here

You also name one Portkey workspace with `--source-scope` (or
`$PORTKEY_WORKSPACE`). This is the stable Portkey **workspace id, not a
secret** — it's safe in logs, `--help`, and error text. One Portkey
workspace per metergraph app for this MVP.

    # PORTKEY_WORKSPACE=ws-your-workspace-id

Two optional env vars override endpoints. `PORTKEY_BASE_URL` points at a
self-hosted Portkey — include the `/v1` API-version prefix, matching the
default public base `https://api.portkey.ai/v1`; `METERGRAPH_INGEST_URL`
points at a non-default metergraph ingest host (the same host `push` uses):

    # PORTKEY_BASE_URL=https://api.portkey.ai/v1
    # METERGRAPH_INGEST_URL=https://ingest.metergraph.dev

**How windows and resume work.** Each run pulls a fixed logical window
of at most **one hour** (`--max-window-seconds` caps it, 1–3600, default
3600). The metergraph import-sync **server owns all resume state**: it
tracks the checkpoint, applies a **5-minute overlap** between windows,
and hands out a **15-minute renewable lease** per run. The relay keeps
**no local checkpoint files** — nothing to back up, and it's safe to run
the same command from many machines. Overlap re-pulled rows are
deduplicated server-side by source event id, so overlap never
double-counts.

**`--initial-since`** seeds only the *first* run for a workspace (the
server needs a starting point when it has no state yet). Once state
exists the server **ignores** it, so cron can safely pass it on every
run:

    metergraphrelay sync portkey --source-scope ws-acme --initial-since 2026-08-01T00:00:00+00:00

**Cron example** — hourly, customer-managed, no local state, safe for
overlapping runs (passes `--initial-since` every run by design):

    # Hourly customer-managed cron (no local state, safe to overlap runs):
    0 * * * * metergraphrelay sync portkey --source-scope ws-acme --initial-since 2026-08-01T00:00:00+00:00

**Exit behavior (cron-friendly).** Both a **`busy`** lease (another run
already holds it) and a **`caught_up`** server response are clean no-op
**exit 0** — a cron overlap or an idle hour is not an error. A handled
failure **releases the lease and exits nonzero** so cron surfaces it and
the next run resumes cleanly. If the process crashes outright, no cleanup
runs and the server's lease simply expires, freeing the next run.

**A window advances only on a fully successful upload.** By design the
run marks the window complete **only when every row uploads with zero
failures** — there is no poison-row skipping, dead-letter queue, or
partial checkpoint, so nothing is ever silently dropped. If some rows are
**persistently rejected** (for example, a row the ingest API keeps
refusing), the run releases the lease and exits nonzero, and **the same
window stays pending** and is retried on the next cron run. It will keep
failing on that window until you correct the underlying cause, so cron
cannot advance past bad data on its own — investigate the reported error
rather than expecting the next run to skip it.

**High-volume windows.** If a one-hour window holds **more than 50,000
records**, the run splits it **once** into **10 sub-windows with
1-second overlaps** and pulls all ten together (source-event dedup
absorbs the boundary overlaps). This split is one-shot, never recursive:
if any single sub-window *still* exceeds 50,000 records, the run fails
with a clear error rather than splitting further.

**Before running this against your own data:** as in manual mode,
request and response content is uploaded to metergraph, with no opt-out.

Full flag reference: `metergraphrelay sync portkey --help`.

## Development

    git clone https://github.com/VasiliyRad/metergraphrelay
    cd metergraphrelay
    pip install -e ".[dev]"
    pytest
