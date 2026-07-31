# Security Policy

## Scope

This repository is `metergraphrelay`, a command-line tool that pulls
stored trace data from LLM providers and pushes it to a metergraph
server. It is not the metergraph ingest server, price catalog, or
dashboard; those live in
[PioneerSquareLabs/metergraph](https://github.com/PioneerSquareLabs/metergraph),
which has its own security policy covering storage, retention, and
server-side access control.

## Supported Versions

This project has one active release line (`0.x`, pre-1.0). Security
fixes land on the latest published release. There is no long-term
support branch yet.

## Reporting a Vulnerability

Please report suspected vulnerabilities privately through
[GitHub Security Advisories](https://github.com/VasiliyRad/metergraphrelay/security/advisories/new)
rather than opening a public issue. Include the affected version, a
reproduction if you have one, and the impact as you understand it.
We'll acknowledge new reports and work with you on a fix and
coordinated disclosure timeline.

## Threat Model

**`pull openai` only reads from providers, never writes.** Your
`OPENAI_API_KEY` is used exclusively to call OpenAI's own read-only
stored-completions endpoints (`GET /v1/chat/completions`,
`GET /v1/chat/completions/{id}/messages`) — it is never sent anywhere
else, logged, or written to the output file.

**`push` sends one credential to one destination.**
`METERGRAPH_APP_TOKEN` is sent as `Authorization: Bearer <token>` to
`METERGRAPH_INGEST_URL`. The default is the real hosted HTTPS endpoint.
If you override `METERGRAPH_INGEST_URL` to point at a self-hosted
`http://` endpoint (as local development setups typically do), the
token and any row content travel in plaintext on that path. Treat the
token like any other API credential: scope it per use, and don't
commit it.

**Content capture is opt-in and local.** `--include-content` writes
prompt/response text into the local JSONL output file in plaintext.
Without that flag, no message content ever leaves memory. The output
file itself has no special protection beyond your filesystem's own
permissions — treat it like any other file containing potentially
sensitive request/response data once `--include-content` is used.

**Not fail-open, by design.** Unlike an embedded SDK meant to run
silently inside a live application, `metergraphrelay` is invoked
manually and interactively. A failed `pull` or `push` is reported
directly to the user (a clear stderr message and non-zero exit code)
rather than swallowed — silent failure would defeat the point of
running the tool.

**Supply chain.** This project depends on `openai` and `python-dotenv`,
both installed from PyPI under the standard pip trust model. Neither is
vendored or pinned to a specific hash in this repository.
