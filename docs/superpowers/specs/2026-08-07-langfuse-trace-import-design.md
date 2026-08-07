# Langfuse trace import (GENERATION observations) — design spec

Date: 2026-08-07

## Goal

Add `metergraphrelay pull langfuse`: import Langfuse **GENERATION**
observations (the LLM call records Langfuse captures — as distinct from
`SPAN`/`EVENT` observations, which are not call records) into
metergraph-native JSONL rows, in the same shape and via the same
`push` command already used by `pull openai`. The end goal is enabling
cost-savings analysis in metergraph over traffic a customer already has
instrumented with Langfuse, without requiring them to re-instrument.

This is read-only against Langfuse (list/fetch only, no writes) and
manually invoked, matching the existing `pull`/`push` split documented
in `2026-07-31-metergraphrelay-rebrand-push-design.md`: `pull langfuse`
produces a local JSONL file; the existing `push` command uploads it
unchanged.

## Scope

**In scope:**
- Langfuse Cloud and self-hosted **v4+ only** (the versions that serve
  the v2 Observations API).
- `GET /api/public/v2/observations`, filtered to `type=GENERATION`.
- One row per GENERATION observation.

**Explicitly out of scope (not partial/deferred within this feature —
excluded from this design entirely):**
- The legacy (pre-v2) Observations API / any v1 `/api/public/observations`
  fallback for older self-hosted deployments.
- Scores/evals (Langfuse's Scores API) — no eval data is imported.
- `SPAN` and `EVENT` observations as call records — only `GENERATION`
  rows become metergraph rows. (Non-generation observations may still
  matter for trace *context*, e.g. via `parentObservationId` chains, but
  are not imported as standalone rows themselves.)
- Any daemon, background worker, or checkpoint/resume mechanism — this
  is a manually invoked, bounded, one-shot pull, not a continuously
  running sync.

## Auth & config

Reuses the credential plumbing already stubbed in `config.py` — no
changes needed there:

| env var | purpose |
|---|---|
| `LANGFUSE_PUBLIC_KEY` | HTTP Basic Auth username |
| `LANGFUSE_SECRET_KEY` | HTTP Basic Auth password |
| `LANGFUSE_HOST` | base URL; defaults to Langfuse Cloud (`https://cloud.langfuse.com`) when unset |

- Auth is HTTP Basic, `LANGFUSE_PUBLIC_KEY` as username,
  `LANGFUSE_SECRET_KEY` as password — verified against Langfuse's public
  API docs, matches the credential pair `config.py` already expects for
  the `"langfuse"` target.
- CLI flags exist as an override path for all three (credentials +
  base URL — see CLI section below) for scripting/CI convenience, but
  **`.env`/environment is the preferred, documented path**; flags are
  the escape hatch, not the primary interface, consistent with every
  other `pull`/`push` subcommand's `--env-file` convention.
- Secrets are never logged or persisted anywhere: not in the output
  JSONL, not in stdout/stderr messages (error paths print the *name* of
  a missing var, never its value — matching `require_credentials`'s
  existing behavior), not written to any state/cache file. There is no
  state/cache file at all (see "Repeated pulls are stateless" below).

## Architecture

- **Direct REST, Python stdlib** (`urllib.request`), the same style
  already used by `push.py` — no new runtime dependency. See
  "Alternatives considered" for why this was chosen over `httpx` or the
  official `langfuse` SDK.
- **Injectable/testable HTTP boundary**: the HTTP-calling function takes
  an injectable transport (or is itself trivially mockable, mirroring
  how `pull_openai` takes an already-constructed `client` rather than
  constructing one internally) so tests never need real credentials or
  network access — same pattern as `tests/providers/test_openai.py`,
  which mocks `client.chat.completions.list`.
- **Cursor pagination**: `page`/`limit` query params against
  `GET /api/public/v2/observations?type=GENERATION`, following the
  `meta` block in each response for continuation, per Langfuse's
  documented pagination contract (default limit 50, max 1000 per page).
- **Normalize**: each GENERATION observation → one metergraph-native row
  dict, via a pure function analogous to `normalize_completion` in
  `providers/openai.py`, independently unit-testable without touching
  HTTP.
- **Atomic JSONL write**: unlike `pull_openai`'s `open(output_path, "w")`
  (which truncates immediately and writes incrementally — meaning a
  failure partway through a pull already leaves a partial/corrupt file
  on disk today), `pull langfuse` writes to a temp file in the same
  directory and `os.replace()`s it over `--output` **only after the
  entire pull succeeds**. This is a deliberate, stricter behavior than
  today's `pull_openai`, justified by the failure-semantics requirement
  below (no partial destination file on a fatal failure) — not a silent
  behavior change to the existing OpenAI path, which is left as-is.
- **`push` is unchanged.** It already treats each JSONL line as opaque
  (`push.py:11-60`) — no schema-awareness to add there. The row shape
  produced by `pull langfuse` must conform to what `push`/metergraph's
  `/v1/ingest` already accepts (verified against
  `metergraph-internal/app/src/metergraph_app/api/main.py:859-906` and
  `metergraphsdk/python/src/metergraph/_capture.py:752-764`, both
  sibling repos on this machine — see Mapping below).

## CLI

```
metergraphrelay pull langfuse [-n/--count 100] [--since ISO8601] [--until ISO8601]
                               [--environment ENV] [--route ROUTE]
                               [--base-url URL] [--output ./traces.jsonl]
                               [--env-file .env]
                               [--langfuse-public-key KEY] [--langfuse-secret-key KEY]
```

| flag | default | notes |
|---|---|---|
| `-n`/`--count` | `100` | cap on rows imported this run — a manual, bounded pull, same spirit as `pull openai`'s `-n` |
| `--since` | none (no lower bound) | `fromTimestamp`; omitted means "as far back as Langfuse has data" |
| `--until` | the wall-clock time the command started running | `toTimestamp`; captured once at invocation, not re-evaluated per page, so a long-running paginated pull has a stable, reproducible window even if new generations land in Langfuse mid-run |
| `--environment` | none | passed through to Langfuse's `environment` filter, if set |
| `--route` | none — falls back to trace/generation name (see Mapping) | same semantics as `pull openai --route`: caller-supplied override |
| `--base-url` | none — falls back to `LANGFUSE_HOST` env, then Langfuse Cloud | CLI override path for self-hosted instances, per the "flags are the escape hatch" rule above |
| `--output` | `./traces.jsonl` | matches `pull openai`'s default |
| `--env-file` | `.env` | matches every existing subcommand |
| `--langfuse-public-key` / `--langfuse-secret-key` | none | CLI override path for credentials; env is preferred |

No `sync langfuse` in this design — `sync openai` exists because pushing
immediately after a fresh pull is a common single-provider loop; nothing
here precludes adding `sync langfuse` later by composing `pull langfuse`
+ the existing `push`, but it's not part of this spec.

## Documentation deliverables

Added per user review: README and CLI `--help` text are **required
deliverables of this feature**, not follow-up polish — see "Acceptance
criteria / implementation handoff" below. Both must exist before this
feature is considered done.

### README

Add a `pull langfuse` section to `README.md`, positioned alongside the
existing `pull openai` documentation and following the same terse,
example-driven style already used there. It must cover, explicitly:

- **Quickstart**: a minimal runnable example, e.g.
  `metergraphrelay pull langfuse -n 25 --output traces.jsonl` followed by
  `metergraphrelay push traces.jsonl` — mirroring the existing
  `pull openai`/`push` two-step quickstart already in the README.
- **Credential/env configuration**: `LANGFUSE_PUBLIC_KEY` and
  `LANGFUSE_SECRET_KEY` in `.env`, matching the `.env` block already
  shown in the README's Setup section (which today only lists
  `OPENAI_API_KEY`/`METERGRAPH_APP_TOKEN` — this needs to grow to
  include the Langfuse pair, or clearly point to `.env.example`, which
  already lists both).
- **Cloud vs self-hosted host configuration**: `LANGFUSE_HOST` unset →
  Langfuse Cloud; set (or `--base-url` passed) → that self-hosted
  instance. State the default explicitly rather than leaving it
  implicit, since this is the one piece of config that differs in kind
  (a URL, not a secret) from every other credential in this tool.
  Verified default: `https://cloud.langfuse.com` (see "Auth & config").
  Note that the URL Langfuse's docs currently cite may drift — confirm
  the live current Cloud default at implementation time rather than
  copying this spec's value unverified into the README.
- **Command examples/defaults**: at least one example using `--since`/
  `--until`, and a explicit statement of `--count`'s default (`100`) —
  the full flag table from the CLI section above, in prose or table
  form, so a reader doesn't have to guess a default by trial and error.
- **v4+ requirement**: state plainly that self-hosted Langfuse must be
  v4+ (the version serving the v2 Observations API) — older self-hosted
  instances are not supported (see Scope, API freshness).
- **Generation-only / no-evals scope**: state plainly that only
  `GENERATION` observations (LLM call records) are imported —
  `SPAN`/`EVENT` observations and Langfuse Scores/evals are not (see
  Scope).
- **Prominent content-transfer/privacy warning**: the same substance as
  "Explicit content-transfer warning" below, written prominently enough
  in the README that a reader encounters it before running the command
  — not buried at the end of a long section. The existing README already
  has a precedent for this pattern (the "Before enabling this in
  production" callout under "Enabling storage on your own calls" for
  the OpenAI path) — follow that same visibility convention here.

### CLI `--help` text

Every flag in the CLI table above must have complete, accurate
`argparse` help text — defaults and env-var relationships stated in the
text itself, not left to be inferred from behavior. Draft text (exact
`argparse` wrapping/formatting is implementation detail; the content
below is what must be present, in substance, not verbatim):

```
usage: metergraphrelay pull langfuse [-h] [-n COUNT] [--since SINCE]
                                      [--until UNTIL] [--environment ENVIRONMENT]
                                      [--route ROUTE] [--base-url BASE_URL]
                                      [--output OUTPUT] [--env-file ENV_FILE]
                                      [--langfuse-public-key KEY]
                                      [--langfuse-secret-key KEY]

Pull Langfuse GENERATION observations (LLM call records) into a local
JSONL file shaped for metergraph's ingest API. SPAN/EVENT observations
and scores/evals are not imported. Requires Langfuse Cloud or
self-hosted v4+ (the v2 Observations API). WARNING: generation
input/output content is transferred from Langfuse into the local
output file, and from there into metergraph via `push`, with no
opt-in gate.

options:
  -h, --help            show this help message and exit
  -n COUNT, --count COUNT
                        Maximum number of GENERATION observations to
                        import. (default: 100)
  --since SINCE         Only import observations at or after this ISO
                        8601 timestamp (Langfuse fromTimestamp,
                        inclusive). (default: no lower bound)
  --until UNTIL         Only import observations before this ISO 8601
                        timestamp (Langfuse toTimestamp, exclusive).
                        (default: the time this command started
                        running, captured once for the whole pull)
  --environment ENVIRONMENT
                        Filter to a single Langfuse environment value.
                        (default: no filter, all environments)
  --route ROUTE         Override the metergraph route field for every
                        imported row. (default: the Langfuse trace
                        name, or the generation's own name if the
                        trace has none)
  --base-url BASE_URL   Langfuse API base URL. (default: $LANGFUSE_HOST
                        if set, else Langfuse Cloud)
  --output OUTPUT       Path to write the resulting JSONL file.
                        (default: ./traces.jsonl)
  --env-file ENV_FILE   Path to a .env file to load credentials from.
                        (default: .env)
  --langfuse-public-key KEY
                        Langfuse public key (Basic Auth username).
                        Overrides $LANGFUSE_PUBLIC_KEY / .env if given;
                        env/.env is the preferred path.
  --langfuse-secret-key KEY
                        Langfuse secret key (Basic Auth password).
                        Overrides $LANGFUSE_SECRET_KEY / .env if given;
                        env/.env is the preferred path.
```

The `pull` subparser's own subcommand listing (`metergraphrelay pull
--help`) must also gain a one-line `help=` string for `langfuse`
alongside the existing `openai`/`anthropic` entries, e.g. "Pull
GENERATION call records from Langfuse (v2 Observations API,
Cloud/self-hosted v4+); no evals/spans/events" — short enough to fit
the subcommand-listing format `build_parser()` already uses for the
other two.

## Mapping

Each GENERATION observation → one metergraph-native row:

| metergraph field | source | notes |
|---|---|---|
| `ts` | `startTime` | ISO 8601, ISO-fromatted directly from Langfuse's own timestamp — no reinterpretation needed (unlike OpenAI's Unix-epoch `created`) |
| `source` | fixed `"langfuse"` | **Resolved per user review** (was an open question). Records which observability platform this row was relayed *through* — Langfuse — distinct from `provider` (the underlying LLM vendor) and from `sdk` (the relay tool). `source` is not one of metergraph's first-class `CALL_COLUMNS`; verified against `metergraph-internal/app/src/metergraph_app/worker/main.py:97-101,336-344,408`, any row field outside the worker's `KNOWN_FIELDS` set is captured automatically into that row's freeform `meta` JSON blob rather than dropped — so `source="langfuse"` needs no server-side schema change, but is queryable via `meta`, not as a dedicated column, unlike `provider`. |
| `sdk` | fixed `"metergraphrelay"` | **Unchanged from `pull openai`'s existing convention** — identifies the relay tool that produced the row, exactly as it does for the OpenAI path. Langfuse generally cannot reliably identify which SDK (if any) the original application used to call the LLM — trace/observation data doesn't carry that provenance — so `sdk` continues to mean "the tool that performed this pull," never "the caller's original instrumentation," for every provider this tool supports. |
| `sdk_version` | this tool's `__version__` (the running relay's version) | unchanged convention from `pull openai` |
| `provider` | (1) explicit provider metadata on the observation/trace if Langfuse has captured one, else (2) conservative model-family inference from `providedModelName` (e.g. a `gpt-`/`o1-`/`o3-` prefix → `"openai"`, `claude-` → `"anthropic"` — illustrative, not exhaustive; the concrete prefix table is an implementation-time task, not invented here), else (3) literal `"unknown"` | Never silently mis-assign a provider; `"unknown"` is a valid, honest output |
| `model` | `providedModelName` | the model name as the caller originally supplied it to Langfuse, not any internally-canonicalized name |
| `input_tokens` / `output_tokens` | `usage` field group | **Unverified exact key names** inside the `usage` object (Langfuse's v2 docs confirm a `usage` field group exists and covers token/cost data, but the precise field names weren't confirmed via API docs in this pass — approved for later per §"API freshness" below); implementation must confirm against a live `fields=usage` response before writing the normalize function |
| `cost_usd` | `totalCost`, used as-is | **No recomputation, no reconciliation against metergraph's own pricing catalog.** Verified against `metergraph-internal/app/src/metergraph_app/migrations/0038_managed_catalog_pricing.sql:19,31` and `metergraph-internal/brain/decisions/2026-07-20-managed-catalog-pricing-authority.md:14`: a client-supplied `cost_usd` on ingest is retained server-side purely as `reported_cost_usd` *provenance* — it does not override metergraph's own catalog-computed cost and cannot price an unpriced/unknown model. This is the correct, already-supported field name and behavior; no server-side change implied. |
| `request_json` / `request_text` | observation `input` | Mapped **losslessly**: if `input` is already a list of role/content messages (the same shape OpenAI's `messages.list` returns), serialize to `request_json` following the existing `pull openai` convention exactly; otherwise (arbitrary/non-chat-shaped input, which Langfuse permits since it's provider-agnostic) serialize to `request_text` instead of forcing a shape that doesn't fit. Both fields are real, already-accepted ingest fields — confirmed at `metergraph-internal/app/src/metergraph_app/api/main.py:886` (`enforce_content_capture` checks `request_json`, `request_text`, and `response_text` as the three content-carrying fields). |
| `response_text` | observation `output` | same as `pull openai`'s `response_text` |
| `route` | `--route` if passed, else trace `name` (`traceName`), else the generation/observation's own `name` if the trace has none | matches `pull openai`'s "caller override, else a reasonable default" pattern |
| `trace_id` | `traceId` | verified real, already-accepted native field — `metergraphsdk/python/src/metergraph/_capture.py:754`, `metergraph-internal` ingest path |
| `span_id` | observation `id` | **Stable and reused across repeated pulls** — unlike the SDK's own `span_id` (`_capture.py:625`, a fresh random hex generated per capture), Langfuse's observation `id` is a durable identifier for the same underlying event every time it's re-fetched. This is the basis for the "stable IDs, no dedup promise" note below. |
| `parent_span_id` | `parentObservationId` | verified real native field, `_capture.py:756` |
| `session_id` | trace `sessionId` (lives on the **trace**, not the observation itself — requires the `trace_context` field group on the v2 endpoint, per Langfuse's v2 API changelog) | verified real native field, `_capture.py:752` |
| `error` (bool) / `error_type` | `level == "ERROR"` → `error=true`, `error_type` set from `statusMessage`; any other `level` (`DEFAULT`/`DEBUG`/`WARNING`) → `error=false`, `error_type=null` | mirrors the existing `error`/`error_type` pair's meaning in `normalize_completion` (a flag distinct from `status`) |
| `status` | `"success"` unless `error` is true, matching `error` → `"error"` | Langfuse, unlike OpenAI's stored-completions list API, **can** represent genuinely failed calls — this is new territory relative to `pull openai`, where `status` is always `"success"` by construction |
| `tags` | observation/trace tags | retained, same field name/shape as `pull openai`'s `tags` |
| `environment` | Langfuse `environment` | native field already exists per the rebrand spec's field inventory; not previously populated by any pull path |
| (name metadata) | trace/observation `name` | retained into `tags` if not already consumed as the `route` fallback, so it isn't silently dropped when `route` comes from elsewhere |

### Explicit content-transfer warning

**Unlike `pull openai`, there is no `--include-content` opt-in gate for
this provider** — the CLI surface above has no such flag. Every
GENERATION observation's `input`/`output` is transferred from Langfuse
into the local JSONL file (and from there into metergraph via `push`)
by default. This must be stated plainly wherever this command is
documented (README, `--help` text, and the CLI's own startup output):
**running `pull langfuse` moves prompt/response content out of Langfuse
and into a local file and then into metergraph, with no per-run
confirmation step.** This is a deliberate scope decision approved for
this spec, not an oversight — flagged here so it's visible rather than
discovered later. If a content opt-out becomes a requirement, it is a
follow-up, not part of this design.

## Statelessness / no deduplication promise

`pull langfuse` is **stateless between invocations**, matching
`pull_openai`'s existing "always overwrite `--output`" behavior — there
is no persisted cursor, last-seen-timestamp, or local ledger. Each run
is a fresh, manually-bounded query over `[--since, --until]`.

`span_id` (Langfuse observation `id`) is stable and identical across
repeated pulls of the same underlying generation (see Mapping above) —
this **supplies** a stable identifier that a downstream system *could*
use for deduplication. It does **not** mean this tool or metergraph's
ingest path promises deduplication. Re-running `pull langfuse --since
<overlapping window>` followed by `push` will re-send already-imported
rows, and whether metergraph's `/v1/ingest` deduplicates by any field is
outside this tool's control and not verified as part of this design
(the same caveat already applies to `pull openai`, which has always had
this property — nothing new is being introduced here, just made
explicit for a case where a stable ID makes dedup newly *possible*
rather than clearly *impossible*).

## Failure semantics

**Fatal** (abort the whole pull, non-zero exit, clear stderr message,
**no changes to the existing `--output` file** — see atomic write in
Architecture):
- Missing/invalid credentials (existing `ConfigError` path, unchanged).
- Auth failure (401/403 from Langfuse).
- Network failure (connection error, timeout, DNS failure).
- Pagination failure (a page request fails mid-pull, or the `meta`
  cursor is malformed/unparseable).
- Invalid/unexpected top-level response shape (e.g. missing `data`/
  `meta`, non-JSON body) — including the "unsupported older deployment"
  case, see API freshness below.

**Non-fatal, per-record** (skip the individual malformed record, print
a warning to stderr naming the observation `id` where available, keep
going):
- A single GENERATION observation that fails to normalize (e.g.
  missing an expected field the normalize function requires).

At the end of a run, print a summary: rows **imported** vs rows
**skipped**, so a partially-clean pull is never silently reported as
fully clean. The `--output` file is only written (atomically replacing
any prior file at that path) if the pull as a whole completes — a
fatal failure anywhere leaves the destination exactly as it was before
the run, but individual skipped records do not block completion or
prevent the file from being written.

## Pagination

- Page size: as large as practical up to Langfuse's documented maximum
  of `1000` per page.
- Stop condition: whichever comes first —
  1. `count` rows have been collected (the `-n/--count` cap), or
  2. the `[--since, --until]` window is exhausted (Langfuse returns a
     page with no further cursor / fewer than the requested page size
     with no more matching data).
- No auto-pagination past `count`, mirroring `pull_openai`'s existing,
  explicitly tested behavior (`test_pull_openai_does_not_paginate_past_count`)
  of capping consumption even when the underlying page/iterator would
  keep yielding more.

## Testing

All tests run against a mocked HTTP boundary — **no live Langfuse
credentials or network access required**, matching the existing
`tests/providers/test_openai.py` pattern of mocking the provider client
rather than hitting a real API. Coverage to include:

- **Auth**: outgoing request carries correct HTTP Basic `Authorization`
  header built from `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`.
- **Base URL selection**: Langfuse Cloud default when `LANGFUSE_HOST`/
  `--base-url` unset; override respected when either is set; `--base-url`
  takes precedence over `LANGFUSE_HOST` per the "flags are the escape
  hatch" rule.
- **Pagination**: multi-page response sequences are followed correctly;
  stops at `count`; stops at window exhaustion when `count` isn't hit.
- **Optional/default bounds**: `--since` omitted → no `fromTimestamp`
  sent; `--until` omitted → captured command-start time is sent and is
  stable across a multi-page pull (not re-evaluated per page).
- **`--count` enforcement**: never imports more than requested even if
  more data is available (same style as the existing OpenAI test).
- **Mapping/normalize correctness**: field-by-field assertions against
  a fake observation object/dict, mirroring
  `test_normalize_completion_with_content_included`'s exact-dict-equality
  style.
- **Provider inference**: explicit-metadata case, model-family-prefix
  case, and the `"unknown"` fallback case, each asserted independently.
- **Content mapping**: chat-message-shaped `input` → `request_json`;
  non-chat-shaped `input` → `request_text`; both are mutually exclusive
  in a given row.
- **Malformed-record skip**: one bad record among several good ones is
  skipped with a stderr warning naming its `id`; the good records still
  import; the run still succeeds (non-fatal path).
- **Fatal failures**: each of missing credentials, 401/403, network
  error, malformed pagination cursor, and unrecognized/invalid response
  shape independently produce a non-zero exit, a clear stderr message,
  and **no change to a pre-existing `--output` file** (atomicity check —
  write a sentinel file at `--output` first, run a pull that fails
  partway through, assert the sentinel content is untouched).
- **CLI-level tests**: `metergraphrelay pull langfuse ...` dispatch,
  flag parsing, and defaults, all with the HTTP layer mocked at the
  same seam as the unit tests above — same layering `test_cli.py`
  already uses (`patch("metergraphrelay.cli.pull_openai", ...)` style)
  for the OpenAI path.
- **CLI help documents the interface** (added per user review): a test
  captures `metergraphrelay pull langfuse --help` output (e.g. via
  `build_parser().parse_args(["pull", "langfuse", "--help"])` catching
  `SystemExit`, or `argparse`'s `format_help()` directly) and asserts it
  contains every flag from the CLI table above, each flag's stated
  default, and the env-var relationship text for `--base-url`,
  `--langfuse-public-key`, and `--langfuse-secret-key`. This is a
  regression guard: a future flag added/renamed/removed without a
  matching help-text update fails this test, keeping `--help` from
  silently drifting away from this spec and the README.
- **Documentation-consistency check** (added per user review): where
  practical, keep README examples from silently rotting relative to the
  implemented interface. Concretely: every `pull langfuse` command shown
  in the README is parsed (not necessarily executed against a live
  Langfuse) via `build_parser().parse_args([...])` using the exact flags
  from that example, asserting it parses without error — so a doc
  example using a since-renamed/removed flag fails a test instead of
  quietly going stale. Full end-to-end execution of README examples
  against live Langfuse data is out of scope for this test (no live
  credentials in CI, per the "no live Langfuse credentials or network
  access required" rule above) — parsing/dispatch-level consistency is
  the practical bar, not live-output equivalence.

## API freshness / version compatibility

This design targets Langfuse's **current** v2 Observations API as
documented at the time of writing (2026-08-07) and Langfuse **self-hosted
v4+**, which is required to serve that API. Two compatibility risks are
named explicitly rather than left implicit:

1. **Older self-hosted deployments** (pre-v4, no v2 Observations API):
   a request to `/api/public/v2/observations` against such a deployment
   will fail (404, or a differently-shaped response). This must surface
   as a **clear, specific fatal error** — e.g. "this Langfuse deployment
   does not support the v2 Observations API; self-hosted v4+ is
   required" — not a generic JSON-parse traceback. Legacy-API fallback
   support is explicitly out of scope (see Scope), so the correct
   behavior on detection is a clean failure, not a degraded pull.
2. **Field-name/schema drift**: several exact field names inside the
   `usage` field group were not independently verified against live API
   docs during this design pass (see the `input_tokens`/`output_tokens`
   row in Mapping). Implementation must confirm these against a real
   `fields=usage` (or equivalent) response — either via Langfuse's
   OpenAPI spec directly or a live sandboxed call — before finalizing
   the normalize function, rather than shipping a guessed field name.

## Alternatives considered

**A. Direct REST via Python stdlib (`urllib.request`) — recommended.**
Zero new runtime dependency; matches `push.py`'s existing hand-rolled
style exactly, so the codebase gains one consistent HTTP-calling idiom
rather than two; keeps `pyproject.toml`'s deliberately minimal
dependency list (`openai`, `python-dotenv` today) unchanged. Cost: more
code to write and test for Basic Auth headers, cursor pagination, and
query-param construction than a client library would provide for free.

**B. `httpx`.** A real, well-maintained HTTP client with a nicer API
than raw `urllib` (timeouts, retries-if-wanted, cleaner error types) —
a plausible middle ground between A and C. Cost: a new dependency for a
tool whose entire value proposition so far has been "no SDK, no
instrumentation beyond credentials" (README's own framing); adds
install/version-pinning surface for a capability stdlib already covers,
if less ergonomically.

**C. Official `langfuse` Python SDK.** Would offload correct
field-name/pagination/auth handling to a maintained client, directly
closing the "unverified `usage` field names" gap noted above. Cost: the
SDK is built around *instrumenting* traces (the write/capture side),
not reading them back out — the read-only slice this tool needs may be
a small, possibly awkwardly-exposed corner of a much larger package;
introduces a second, differently-shaped dependency (SDK object model)
alongside the `openai` client already in use; version-pinning risk if
Langfuse ships SDK changes independent of API changes.

**Recommendation:** A, for consistency with `push.py`'s existing style
and the project's stated minimal-dependency posture — but this is the
one open trade worth revisiting if the `usage`-field-name verification
in "API freshness" above turns out to be unexpectedly costly to pin
down via docs/spec alone.

## Security & privacy

- Credentials (`LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`) are read
  from environment/`.env` only, sent solely as the Basic Auth header to
  the configured Langfuse host, never logged, never written to the
  output JSONL or any other file.
- Basic Auth over a non-HTTPS `--base-url`/`LANGFUSE_HOST` (a
  self-hosted instance reachable only over plain `http://`) would send
  both the secret key and all pulled content in plaintext on that leg —
  the same caveat already documented in `SECURITY.md` for metergraph's
  own `METERGRAPH_INGEST_URL` self-hosted override. Worth carrying the
  same explicit callout into `SECURITY.md` when this ships.
- See "Explicit content-transfer warning" above: this command moves
  prompt/response content by default, with no opt-in gate — the
  privacy-relevant behavior a user most needs to know before running it.

## Acceptance criteria / implementation handoff

Added per user review, so README and CLI help are explicit,
non-optional parts of this feature's definition of done rather than
follow-up polish. This feature is complete only when all of the
following hold:

- [ ] `pull langfuse` is implemented per Architecture, CLI, Mapping, and
      Failure semantics above.
- [ ] Tests exist per the Testing section above, including the two
      documentation-specific items added in this review pass (CLI help
      content, README example parse-consistency).
- [ ] `README.md` has a `pull langfuse` section covering: quickstart,
      credential/env configuration, Cloud vs. self-hosted host
      configuration, command examples with defaults stated, the v4+
      self-hosted requirement, the generation-only/no-evals scope
      statement, and a prominent content-transfer/privacy warning — all
      per "Documentation deliverables → README" above.
- [ ] `metergraphrelay pull langfuse --help` and `metergraphrelay pull
      --help`'s `langfuse` subcommand line are complete per
      "Documentation deliverables → CLI `--help` text" above — every
      flag, its default, and (for `--base-url`/credential flags) its
      env-var relationship, stated in the help text itself.
- [ ] `.env.example` already lists `LANGFUSE_PUBLIC_KEY`/
      `LANGFUSE_SECRET_KEY` (verified pre-existing); confirm at
      implementation time whether `LANGFUSE_HOST` should be added there
      too, since it's a new env var this feature introduces that
      `.env.example` doesn't yet mention.

A PR implementing this design that lands `pull langfuse` code without
the README section and complete `--help` text is **not** a complete
implementation of this spec — both are part of the same handoff, not a
separate follow-up task.

## Non-goals

- Legacy (pre-v2) Observations API / self-hosted deployments older than
  v4.
- Scores/evals import.
- `SPAN`/`EVENT` observations imported as their own rows.
- Any daemon, scheduler, checkpoint, or continuous/incremental sync
  mechanism.
- A deduplication guarantee, on either this tool's or metergraph's side.
- A `sync langfuse` convenience command (composing `pull langfuse` +
  existing `push` remains possible manually).
- A `--include-content`-style opt-out for content transfer.
- Client-side request batching (same non-goal already stated for `push`
  in the rebrand spec).

## Open questions (carried forward, not blocking this spec)

- Confirm exact `usage` field-group key names via live API/OpenAPI spec
  before writing the normalize function.
- Confirm whether `sessionId` is reliably present via the `trace_context`
  field group on every observation, or only when a trace explicitly
  sets one (affects whether `session_id` should ever be treated as
  required vs. always-optional in tests).
