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
    metergraphrelay pull braintrust --project my-project -n 25 --output my-traces.jsonl
    metergraphrelay pull phoenix --project my-project -n 25 --output my-traces.jsonl
    metergraphrelay sync portkey export.jsonl --output converted.jsonl
    metergraphrelay sync langfuse --initial-since 2026-08-01T00:00:00+00:00 --tag prod
    metergraphrelay sync braintrust --initial-since 2026-08-01T00:00:00+00:00 --project my-project
    metergraphrelay sync phoenix --initial-since 2026-08-01T00:00:00+00:00 --project my-project

`sync openai` accepts the same flags as `pull openai`. It pulls to
`--output` and immediately pushes that same file, checking both
`OPENAI_API_KEY` and `METERGRAPH_APP_TOKEN` upfront so it fails fast
instead of pulling data it can't push.

`pull anthropic` accepts the same shape but isn't implemented yet — it
checks for `ANTHROPIC_API_KEY` and reports accordingly. `pull langfuse`
is implemented — see [Pull from Langfuse](#pull-from-langfuse) below —
as is `pull braintrust`, see
[Pull from Braintrust](#pull-from-braintrust), and `pull phoenix`, see
[Pull from Arize Phoenix](#pull-from-arize-phoenix).
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

## Pull from Braintrust

Import Braintrust **LLM spans** — the spans Braintrust marks with
`span_attributes.type = 'llm'`, i.e. the model calls — into the same
metergraph-native JSONL shape as `pull openai`. Only LLM spans are
imported: `task`/`tool`/`function`/`eval`/`score`/`review` spans are
application and eval structure, not model calls, and Braintrust
scores/evals are never imported.

This reads Braintrust's **`POST /btql`** query endpoint, the same
endpoint the SQL sandbox and `bt sql` use, with a
`FROM project_logs(..., shape => 'spans')` query per project.

**Setup:** add your Braintrust API key to `.env`:

    BRAINTRUST_API_KEY=...

By default this talks to Braintrust's **US** data plane. For the EU data
plane or a self-hosted deployment, set `BRAINTRUST_BASE_URL` in `.env`
(or pass `--base-url` per-command):

    BRAINTRUST_BASE_URL=https://api-eu.braintrust.dev

**Quickstart:**

    metergraphrelay pull braintrust --project my-project -n 25 --output traces.jsonl
    metergraphrelay push traces.jsonl

`--project` is **required** and repeatable. Each value may be a project
**name or a project id** — Braintrust's `project_logs()` accepts either —
and multiple projects are queried together in one pass.

**Narrowing what gets pulled**, beyond `-n`/`--count`:

    metergraphrelay pull braintrust --project my-project --since 2026-08-01T00:00:00Z --until 2026-08-07T00:00:00Z

- `--since`/`--until` bound the span's `created` timestamp (`--since` is
  inclusive, `--until` exclusive). `--until` defaults to the moment the
  command started, captured once for the whole pull.
- **Pass `--since` on anything but a small project.** Braintrust warns
  that a `project_logs()` query with no lower time bound scans the
  project's entire history, and `/btql` fails a query server-side at 30
  seconds.
- `--count` is always a cap on the number of **LLM spans** imported,
  never a count of distinct traces. Results are paged through
  Braintrust's cursor (`x-bt-cursor`), newest first.

**Field mapping notes.**

- `route` defaults to the LLM span's own name (e.g.
  `"OpenAI Chat Completion"`), falling back to `braintrust/backfill`.
  A Braintrust trace has no name of its own, and the root span that
  would carry a workflow name is a different row this query doesn't
  return. `--route` overrides it for every imported row, and the span
  name is then preserved under `tags.name`.
- Token counts come from Braintrust's normalized span metrics
  (`prompt_tokens`, `completion_tokens`, `prompt_cached_tokens`,
  `prompt_cache_creation_tokens`, `completion_reasoning_tokens`).
  Braintrust's convention already matches metergraph's — `prompt_tokens`
  is the **total**, with cache reads and writes as subsets of it — so
  the counts are carried across unchanged.
- `cost_usd` comes from Braintrust's `estimated_cost()`, which returns a
  logged `metrics.estimated_cost` when there is one and otherwise
  derives cost from token metrics and model-registry pricing.
- `latency_ms` is derived from the span's `metrics.start`/`metrics.end`.
- Braintrust span `tags` land under `tags.braintrust_tags`, and the
  source project under `tags.braintrust_project_id`.

**Before running this against your own data:** `pull braintrust`
transfers every matched span's input/output content from Braintrust into
your local JSONL file, and from there into metergraph via `push`, with
no separate opt-in step — unlike `pull openai`'s `--include-content`
flag, there is no way to pull Braintrust spans without their content.
The query explicitly disables Braintrust's preview truncation so the
content that arrives is the full logged content, not a clipped preview.

There is no `sync braintrust` cron mode yet. Adding one would reuse the
same server-owned lease machinery
[`sync portkey`](#sync-from-portkey-api-cron-mode) already uses, plus
one change on the metergraph server: adding `"braintrust"` to the
import-sync source allowlist.

Full flag reference: `metergraphrelay pull braintrust --help`.

## Pull from Arize Phoenix

Import Phoenix **LLM spans** — the OpenInference spans with
`span_kind = LLM`, i.e. the model calls — into the same metergraph-native
JSONL shape as `pull openai`. Only LLM spans are imported:
`CHAIN`/`TOOL`/`RETRIEVER`/`AGENT` spans are application structure, not
model calls, and Phoenix annotations and evals are never imported.

This reads Phoenix's **`GET /v1/projects/{project}/spans`** endpoint with
cursor pagination and its `span_kind` / `name` filters, which Phoenix added
in **13.15**. An older server ignores unknown query parameters and returns
every span kind; the relay filters those out locally and warns, so nothing
but LLM spans is ever imported, but `--name` has no effect there.

**Setup:** nothing, for a local Phoenix. The default base URL is
`http://localhost:6006`. For a remote or authenticated Phoenix, set these
in `.env` (or pass `--base-url` / `--phoenix-api-key` per-command):

    PHOENIX_BASE_URL=https://phoenix.example.com
    PHOENIX_API_KEY=...

**Quickstart:**

    metergraphrelay pull phoenix --project my-project -n 25 --output traces.jsonl
    metergraphrelay push traces.jsonl

`--project` is **required** and repeatable. Each value may be a project
**name or id**; projects are read in the order given and share one
`--count` cap.

**Narrowing what gets pulled**, beyond `-n`/`--count`:

    metergraphrelay pull phoenix --project my-project --since 2026-08-01T00:00:00Z --until 2026-08-07T00:00:00Z
    metergraphrelay pull phoenix --project my-project --name support-desk/triage --name support-desk/draft-reply

- `--since`/`--until` bound the span's start time (`--since` inclusive,
  `--until` exclusive). `--until` defaults to the moment the command
  started, captured once for the whole pull.
- `--name` matches the span name. Repeatable; multiple values are **OR'd**.
- `--count` is always a cap on the number of **LLM spans** imported, never
  a count of distinct traces. Results come back most recently ingested
  first, which for live traffic is newest first.

**Field mapping notes.**

- `route` is resolved from the most specific source on the span: the
  `metergraph.route` attribute, then `gen_ai.operation.name`, then the
  span's **own name**, then `phoenix/backfill`. A stock OpenInference
  instrumentor sets neither attribute and names the span after the SDK
  method (`ChatCompletion`), so live auto-instrumented traffic lands on
  that name; pass `--route` to override every imported row, and the span
  name is then preserved under `tags.name`.
- Token counts come from the `llm.token_count.*` attributes (`prompt`,
  `completion`, `prompt_details.cache_read`, `prompt_details.cache_write`,
  `completion_details.reasoning`). OpenInference's `prompt` is already the
  **total** with cache reads as a subset, matching metergraph's convention,
  so the counts are carried across unchanged.
- `latency_ms` is derived from the span's start and end time.
- `cost_usd` is left empty: the spans endpoint does not return Phoenix's
  computed cost, and metergraph prices the row from its own catalog.
- Prompt and response come from the flattened `llm.input_messages.*` /
  `llm.output_messages.*` attributes, including content-block text,
  falling back to `input.value` / `output.value`. Tool call names from
  every output message land under `tool_names`.
- The source project lands under `tags.phoenix_project`.

**Before running this against your own data:** `pull phoenix` transfers
every matched span's input/output content from Phoenix into your local
JSONL file, and from there into metergraph via `push`, with no separate
opt-in step — unlike `pull openai`'s `--include-content` flag, there is no
way to pull Phoenix spans without their content.

There is no `sync phoenix` cron mode yet.

Full flag reference: `metergraphrelay pull phoenix --help`.

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

The relay converts Portkey's `created_at` value to RFC 3339 UTC before
uploading it. A missing, invalid, or timezone-ambiguous timestamp fails the
conversion instead of silently assigning the trace to the import date.

**Before running this against your own export:** request and response
content from the export is uploaded to MeterGraph, with no opt-out.

Full flag reference: `metergraphrelay sync portkey --help`.

## Sync from Portkey (API cron mode)

Run `sync portkey` with **no `EXPORT_FILE`** and it pulls a window of
logs directly from the Portkey **Logs Export API** (unlike manual mode,
this contacts Portkey) and pushes them to metergraph in one step. It is
designed to run unattended from cron.

**Setup:** in addition to `METERGRAPH_APP_TOKEN`, this mode requires
`PORTKEY_API_KEY` (a secret) in `.env`. The key must have the
**`logs.export`** scope. Note that Portkey **Logs Export is currently an
Enterprise-plan-only feature** — a key without that entitlement reaches
the API but is rejected at authorization:

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

## Sync from Langfuse, Braintrust and Phoenix (cron mode)

`sync langfuse`, `sync braintrust` and `sync phoenix` run the same
server-coordinated loop as [Portkey's API cron mode](#sync-from-portkey-api-cron-mode),
over each provider's own time-bounded, cursor-paged query. A run acquires a
lease, pulls exactly the window the metergraph import-sync server hands it,
pushes every row, and completes the lease. **All resume state lives on the
server**: it tracks the checkpoint, applies the 5-minute overlap, and holds a
15-minute renewable lease per run. The relay keeps no local files, so the
same cron line is safe from several machines.

    metergraphrelay sync langfuse   --initial-since 2026-08-01T00:00:00+00:00 --tag prod
    metergraphrelay sync braintrust --initial-since 2026-08-01T00:00:00+00:00 --project my-project
    metergraphrelay sync phoenix    --initial-since 2026-08-01T00:00:00+00:00 --project my-project

Each takes the selectors of its `pull` counterpart (`--trace-name`/`--tag`/
`--environment`, `--project`, `--name`) plus `--route`, but **no
`--since`/`--until`/`--count`**: the server chooses the window and every
row in it must land before the checkpoint advances.

**Credentials** are the same as for `pull`: Langfuse needs
`LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`, Braintrust needs
`BRAINTRUST_API_KEY`, a local Phoenix needs nothing; all three need
`METERGRAPH_APP_TOKEN`. `METERGRAPH_INGEST_URL` points every call at a
self-hosted server.

**`--source-scope`** names the checkpoint on the server, one per
`(source, scope)`. It defaults to something stable and non-secret: the
Langfuse public key (it identifies the project and is designed to ship in
client code), or the comma-joined `--project` list for Braintrust and
Phoenix. Pass it explicitly when the same project should be tracked as two
separate streams (for example one cron per Langfuse tag).

**Dedup.** Every synced row carries `import_source`, `import_source_scope`
and `import_event_id` (the Langfuse observation id, the Braintrust row id,
or the Phoenix span id), so a row re-pulled inside the overlap is
deduplicated server-side and never double-counts. `pull` writes no such
identity, so a `pull` followed by a `sync` over the same range does count
twice.

**Exit behavior, windows, and failures** follow Portkey's: `busy` and
`caught_up` exit 0; a failed row releases the lease and exits nonzero with
the same window pending; a lost lease exits nonzero immediately and later
runs see `busy` until the server expires it. Two checks are stricter than
`pull`: a row the provider cannot normalize leaves the window pending (pass
`--allow-skipped` to advance past it, since in sync mode there is no export
file to recover it from), and a row whose provider id cannot serve as an
import identity fails the window before anything uploads. There is no
volume split: a large window simply pages further, and a progress hook fired
per imported row renews the lease as it goes.

**Cron example** — hourly, one line per source:

    0 * * * * metergraphrelay sync langfuse   --initial-since 2026-08-01T00:00:00+00:00 --tag prod
    5 * * * * metergraphrelay sync braintrust --initial-since 2026-08-01T00:00:00+00:00 --project my-project
    10 * * * * metergraphrelay sync phoenix   --initial-since 2026-08-01T00:00:00+00:00 --project my-project

The server must allowlist the source: metergraph servers at or after
migration `0072_import_sync_sources` accept `langfuse`, `braintrust` and
`phoenix`; an older server answers acquire with 422 and the run exits
nonzero without touching anything.

Full flag reference: `metergraphrelay sync <provider> --help`.

## Development

    git clone https://github.com/VasiliyRad/metergraphrelay
    cd metergraphrelay
    pip install -e ".[dev]"
    pytest

### End-to-end checks against a real server

`tests/e2e/` holds contract checks that `pytest` does not run: they need a
live metergraph server, and they assert on what survives ingest rather than
on what the relay writes to disk. CI runs both against the OSS server (see
the `latest-relay-latest-server` job).

To run them locally, start the OSS server — Postgres plus one process,
migrations run on startup:

    docker compose up            # in a metergraph OSS checkout
    pip install -e . -e ../metergraph/server

    MG_URL=http://127.0.0.1:8787 MG_TOKEN=dev-token python tests/e2e/oss_server_timestamp.py
    MG_URL=http://127.0.0.1:8787 MG_TOKEN=dev-token python tests/e2e/oss_server_braintrust.py

`oss_server_braintrust.py` drives the real `pull braintrust` loop against a
stubbed `/btql` page, so it needs no Braintrust credential. To *also* run one
real query against a live Braintrust workspace — the only way to confirm the
query itself is accepted — add:

    BRAINTRUST_API_KEY=... BRAINTRUST_E2E_PROJECT=my-project \
      BRAINTRUST_E2E_SINCE=2026-08-01T00:00:00Z \
      MG_URL=http://127.0.0.1:8787 MG_TOKEN=dev-token \
      python tests/e2e/oss_server_braintrust.py

That live section is skipped whenever `BRAINTRUST_E2E_PROJECT` is unset, and
never runs in CI.
