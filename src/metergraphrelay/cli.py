from __future__ import annotations

import argparse
import os
import sys
import tempfile
from datetime import datetime, timezone

from dotenv import load_dotenv
from openai import OpenAI

from .config import ConfigError, require_credentials
from .demo import run_demo
from .metergraph_sync import MeterGraphSyncClient, MeterGraphSyncError
from .portkey_sync import run_portkey_sync
from .provider_sync import UNBOUNDED_COUNT, run_pull_sync
from .providers.braintrust import (
    DEFAULT_BRAINTRUST_URL,
    BraintrustAPIError,
    pull_braintrust,
)
from .providers.langfuse import DEFAULT_LANGFUSE_HOST, LangfuseAPIError, pull_langfuse
from .providers.phoenix import DEFAULT_PHOENIX_URL, PhoenixAPIError, pull_phoenix
from .providers.openai import pull_openai
from .providers.portkey import convert_portkey_export
from .providers.portkey_export import PortkeyExportClient, PortkeyExportError
from .push import DEFAULT_INGEST_URL, push_file
from .window import normalize_utc_designator


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="metergraphrelay",
        description=(
            "Move LLM trace data between systems: pull stored traces from a "
            "provider into a local JSONL file, then push them to metergraph. "
            "Learn more: https://www.metergraph.dev/"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    pull_parser = subparsers.add_parser(
        "pull", help="Pull trace data from a provider into a local JSONL file"
    )
    pull_subparsers = pull_parser.add_subparsers(dest="provider", required=True)

    pull_openai_parser = pull_subparsers.add_parser("openai")
    pull_openai_parser.add_argument("-n", "--count", type=int, default=10)
    pull_openai_parser.add_argument("--output", default="./traces.jsonl")
    pull_openai_parser.add_argument("--stdout", action="store_true")
    pull_openai_parser.add_argument("--route", default="openai/backfill")
    pull_openai_parser.add_argument("--include-content", action="store_true")
    pull_openai_parser.add_argument("--env-file", default=".env")

    pull_anthropic_parser = pull_subparsers.add_parser("anthropic")
    pull_anthropic_parser.add_argument("-n", "--count", type=int, default=10)
    pull_anthropic_parser.add_argument("--output", default="./traces.jsonl")
    pull_anthropic_parser.add_argument("--env-file", default=".env")

    pull_langfuse_parser = pull_subparsers.add_parser(
        "langfuse",
        description=(
            "Pull Langfuse GENERATION observations (LLM call records) into a "
            "local JSONL file shaped for metergraph's ingest API. SPAN/EVENT "
            "observations and scores/evals are not imported. Requires Langfuse "
            "Cloud or self-hosted v4+ (the v2 Observations API). With no "
            "--trace-name/--tag/--environment/--since/--until given, imports "
            "the latest --count GENERATION observations overall. WARNING: "
            "generation input/output content is transferred from Langfuse "
            "into the local output file, and from there into metergraph via "
            "`push`, with no opt-in gate."
        ),
        help=(
            "Pull GENERATION call records from Langfuse (v2 Observations "
            "API, Cloud/self-hosted v4+); no evals/spans/events"
        ),
    )
    pull_langfuse_parser.add_argument(
        "-n",
        "--count",
        type=int,
        default=100,
        help=(
            "Maximum number of GENERATION observations to import (never a "
            "count of distinct traces). (default: 100)"
        ),
    )
    pull_langfuse_parser.add_argument(
        "--since",
        default=None,
        help=(
            "Only import observations at or after this ISO 8601 timestamp "
            "(Langfuse fromStartTime, inclusive). (default: no lower bound)"
        ),
    )
    pull_langfuse_parser.add_argument(
        "--until",
        default=None,
        help=(
            "Only import observations before this ISO 8601 timestamp "
            "(Langfuse toStartTime, exclusive). (default: the time this "
            "command started running, captured once for the whole pull)"
        ),
    )
    pull_langfuse_parser.add_argument(
        "--trace-name",
        action="append",
        default=None,
        metavar="NAME",
        help=(
            "Only import generations whose trace name is NAME. Repeatable: "
            "multiple --trace-name values are OR'd together (any match). "
            "Combines with --tag/--environment/--since/--until using AND. "
            "(default: no filter, all trace names)"
        ),
    )
    pull_langfuse_parser.add_argument(
        "--tag",
        action="append",
        default=None,
        metavar="TAG",
        help=(
            "Only import generations whose trace has tag TAG. Repeatable: "
            "multiple --tag values require ALL given tags to be present "
            "(AND). Combines with --trace-name/--environment/--since/--until "
            "using AND. Only matches tags that already exist on the data; "
            'omitting --tag means no tag filter, not "untagged only". '
            "(default: no filter, all tags)"
        ),
    )
    pull_langfuse_parser.add_argument(
        "--environment",
        default=None,
        help="Filter to a single Langfuse environment value. (default: no filter, all environments)",
    )
    pull_langfuse_parser.add_argument(
        "--route",
        default=None,
        help=(
            "Override the metergraph route field for every imported row. "
            "(default: the Langfuse trace name, or the generation's own "
            "name if the trace has none) Not a selector — see --trace-name "
            "for filtering which generations are pulled."
        ),
    )
    pull_langfuse_parser.add_argument(
        "--base-url",
        default=None,
        help="Langfuse API base URL. (default: $LANGFUSE_BASE_URL if set, else Langfuse Cloud)",
    )
    pull_langfuse_parser.add_argument(
        "--output",
        default="./traces.jsonl",
        help="Path to write the resulting JSONL file. (default: ./traces.jsonl)",
    )
    pull_langfuse_parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to a .env file to load credentials from. (default: .env)",
    )
    pull_langfuse_parser.add_argument(
        "--langfuse-public-key",
        default=None,
        metavar="KEY",
        help=(
            "Langfuse public key (Basic Auth username). Overrides "
            "$LANGFUSE_PUBLIC_KEY / .env if given; env/.env is the preferred path."
        ),
    )
    pull_langfuse_parser.add_argument(
        "--langfuse-secret-key",
        default=None,
        metavar="KEY",
        help=(
            "Langfuse secret key (Basic Auth password). Overrides "
            "$LANGFUSE_SECRET_KEY / .env if given; env/.env is the preferred path."
        ),
    )

    pull_braintrust_parser = pull_subparsers.add_parser(
        "braintrust",
        description=(
            "Pull Braintrust LLM spans (span_attributes.type = 'llm') from one "
            "or more projects into a local JSONL file shaped for metergraph's "
            "ingest API. Reads Braintrust's POST /btql query endpoint. Only LLM "
            "spans are imported: task/tool/function/eval spans, and scores/"
            "evals, are never imported. WARNING: span input/output content is "
            "transferred from Braintrust into the local output file, and from "
            "there into metergraph via `push`, with no opt-in gate."
        ),
        help=(
            "Pull LLM spans from Braintrust project logs (POST /btql); no "
            "scores/evals, no non-LLM spans"
        ),
    )
    pull_braintrust_parser.add_argument(
        "--project",
        action="append",
        required=True,
        metavar="PROJECT",
        help=(
            "Braintrust project to read logs from, by name or by project id "
            "(both are accepted). Repeatable: multiple --project values are "
            "queried together. Required."
        ),
    )
    pull_braintrust_parser.add_argument(
        "-n",
        "--count",
        type=int,
        default=100,
        help=(
            "Maximum number of LLM spans to import (never a count of distinct "
            "traces). (default: 100)"
        ),
    )
    pull_braintrust_parser.add_argument(
        "--since",
        default=None,
        help=(
            "Only import spans created at or after this ISO 8601 timestamp "
            "(inclusive). Recommended: Braintrust warns that a project_logs "
            "query without a lower time bound scans the whole project history. "
            "(default: no lower bound)"
        ),
    )
    pull_braintrust_parser.add_argument(
        "--until",
        default=None,
        help=(
            "Only import spans created before this ISO 8601 timestamp "
            "(exclusive). (default: the time this command started running, "
            "captured once for the whole pull)"
        ),
    )
    pull_braintrust_parser.add_argument(
        "--route",
        default=None,
        help=(
            "Override the metergraph route field for every imported row. "
            "(default: the LLM span's own name, else braintrust/backfill) Not "
            "a selector — see --project for choosing what is pulled."
        ),
    )
    pull_braintrust_parser.add_argument(
        "--base-url",
        default=None,
        help=(
            "Braintrust API base URL. (default: $BRAINTRUST_BASE_URL if set, "
            "else the US data plane; the EU data plane is "
            "https://api-eu.braintrust.dev)"
        ),
    )
    pull_braintrust_parser.add_argument(
        "--output",
        default="./traces.jsonl",
        help="Path to write the resulting JSONL file. (default: ./traces.jsonl)",
    )
    pull_braintrust_parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to a .env file to load credentials from. (default: .env)",
    )
    pull_braintrust_parser.add_argument(
        "--braintrust-api-key",
        default=None,
        metavar="KEY",
        help=(
            "Braintrust API key (Bearer token). Overrides $BRAINTRUST_API_KEY "
            "/ .env if given; env/.env is the preferred path."
        ),
    )

    pull_phoenix_parser = pull_subparsers.add_parser(
        "phoenix",
        description=(
            "Pull Arize Phoenix LLM spans (OpenInference span_kind = LLM) from "
            "one or more projects into a local JSONL file shaped for "
            "metergraph's ingest API. Reads Phoenix's "
            "GET /v1/projects/{project}/spans endpoint. Only LLM spans are "
            "imported: CHAIN/TOOL/RETRIEVER/AGENT spans, annotations and evals "
            "are never imported. WARNING: span input/output content is "
            "transferred from Phoenix into the local output file, and from "
            "there into metergraph via `push`, with no opt-in gate."
        ),
        help=(
            "Pull LLM spans from Arize Phoenix projects "
            "(GET /v1/projects/{project}/spans); no annotations, no non-LLM spans"
        ),
    )
    pull_phoenix_parser.add_argument(
        "--project",
        action="append",
        required=True,
        metavar="PROJECT",
        help=(
            "Phoenix project to read spans from, by name or by project id "
            "(both are accepted). Repeatable: projects are read in order and "
            "share one --count cap. Required."
        ),
    )
    pull_phoenix_parser.add_argument(
        "-n",
        "--count",
        type=int,
        default=100,
        help=(
            "Maximum number of LLM spans to import (never a count of distinct "
            "traces). (default: 100)"
        ),
    )
    pull_phoenix_parser.add_argument(
        "--since",
        default=None,
        help=(
            "Only import spans that started at or after this ISO 8601 "
            "timestamp (inclusive). (default: no lower bound)"
        ),
    )
    pull_phoenix_parser.add_argument(
        "--until",
        default=None,
        help=(
            "Only import spans that started before this ISO 8601 timestamp "
            "(exclusive). (default: the time this command started running, "
            "captured once for the whole pull)"
        ),
    )
    pull_phoenix_parser.add_argument(
        "--name",
        action="append",
        default=None,
        metavar="SPAN_NAME",
        help=(
            "Only import spans with this name. Repeatable; multiple values are "
            "OR'd. (default: every LLM span)"
        ),
    )
    pull_phoenix_parser.add_argument(
        "--route",
        default=None,
        help=(
            "Override the metergraph route field for every imported row. "
            "(default: the span's metergraph.route or gen_ai.operation.name "
            "attribute, else its own name, else phoenix/backfill) Not a "
            "selector — see --name and --project for choosing what is pulled."
        ),
    )
    pull_phoenix_parser.add_argument(
        "--base-url",
        default=None,
        help=(
            "Phoenix server base URL. (default: $PHOENIX_BASE_URL if set, else "
            f"{DEFAULT_PHOENIX_URL})"
        ),
    )
    pull_phoenix_parser.add_argument(
        "--output",
        default="./traces.jsonl",
        help="Path to write the resulting JSONL file. (default: ./traces.jsonl)",
    )
    pull_phoenix_parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to a .env file to load settings from. (default: .env)",
    )
    pull_phoenix_parser.add_argument(
        "--phoenix-api-key",
        default=None,
        metavar="KEY",
        help=(
            "Phoenix API key (Bearer token), only needed when the server has "
            "authentication enabled. Overrides $PHOENIX_API_KEY / .env if "
            "given. (default: none)"
        ),
    )

    sync_parser = subparsers.add_parser(
        "sync",
        help="Pull trace data from a provider and push it to metergraph in one step",
    )
    sync_subparsers = sync_parser.add_subparsers(dest="provider", required=True)
    sync_openai_parser = sync_subparsers.add_parser("openai")
    sync_openai_parser.add_argument("-n", "--count", type=int, default=10)
    sync_openai_parser.add_argument("--output", default="./traces.jsonl")
    sync_openai_parser.add_argument("--stdout", action="store_true")
    sync_openai_parser.add_argument("--route", default="openai/backfill")
    sync_openai_parser.add_argument("--include-content", action="store_true")
    sync_openai_parser.add_argument("--env-file", default=".env")

    sync_portkey_parser = sync_subparsers.add_parser(
        "portkey",
        description=(
            "Sync Portkey LLM traces to metergraph in one of two modes.\n"
            "\n"
            "MANUAL MODE (an EXPORT_FILE is given): convert a local Portkey "
            "JSONL log export into metergraph-native JSONL and upload it. "
            "Requires a Portkey subscription with log export enabled -- "
            "download the export from Portkey yourself first; manual mode "
            "never contacts Portkey. With no --output, a private temporary "
            "converted file is used and removed after upload; with --output, "
            "the converted file is kept, including if the upload fails.\n"
            "\n"
            "API CRON MODE (no EXPORT_FILE): pull a fixed logical time window "
            "from the Portkey Logs Export API and push it to metergraph, with "
            "acquire/resume/complete coordinated entirely by the metergraph "
            "import-sync server -- safe to run from cron and idempotent, with "
            "no local checkpoint files. Requires PORTKEY_API_KEY (secret) -- a "
            "Portkey key with the logs.export scope; Logs Export is currently an "
            "Enterprise-plan-only feature -- and METERGRAPH_APP_TOKEN (secret) in "
            "the env / --env-file, plus a "
            "workspace via --source-scope or $PORTKEY_WORKSPACE. Optional env: "
            "$PORTKEY_BASE_URL (default the Portkey public API), "
            "$METERGRAPH_INGEST_URL (default the metergraph ingest host). "
            "--initial-since seeds only the first run; --max-window-seconds "
            "caps the window at 3600. One Portkey workspace per metergraph app "
            "(MVP). A 'busy' lease or a 'caught up' server exit 0 as clean "
            "no-ops. In both modes request and response content is uploaded to "
            "MeterGraph with no opt-out."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        help=(
            "Sync Portkey traces to metergraph: manual (local EXPORT_FILE) "
            "or API cron mode (no EXPORT_FILE, pulls from Portkey)"
        ),
    )
    sync_portkey_parser.add_argument(
        "export_file",
        metavar="EXPORT_FILE",
        nargs="?",
        default=None,
        help=(
            "Path to a Portkey JSONL log export already downloaded from "
            "Portkey (manual mode). Omit to use Portkey API cron mode."
        ),
    )
    sync_portkey_parser.add_argument(
        "--source-scope",
        default=None,
        help=(
            "Portkey workspace identifier for API cron mode (falls back to "
            "$PORTKEY_WORKSPACE). This is the stable workspace id, not a "
            "secret. One workspace per metergraph app (MVP)."
        ),
    )
    sync_portkey_parser.add_argument(
        "--initial-since",
        default=None,
        help=(
            "Aware ISO 8601 timestamp seeding the first sync window (API "
            "mode). Required only on the very first run; the server ignores "
            "it once state exists, so cron may pass it every run."
        ),
    )
    sync_portkey_parser.add_argument(
        "--max-window-seconds",
        type=int,
        default=None,
        help=(
            "Maximum logical window length in seconds (API mode, 1-3600). "
            "(default: 3600)"
        ),
    )
    sync_portkey_parser.add_argument(
        "--output",
        default=None,
        help=(
            "Path to write the converted metergraph-native JSONL (manual "
            "mode only). (default: a private temporary file, removed after "
            "upload)"
        ),
    )
    sync_portkey_parser.add_argument(
        "--env-file",
        default=".env",
        help=(
            "Path to a .env file to load credentials from: METERGRAPH_APP_TOKEN "
            "(both modes) and PORTKEY_API_KEY (API mode). (default: .env)"
        ),
    )

    # --- sync langfuse / braintrust / phoenix: server-coordinated cron mode ---
    # One shape for the three cursor-paged providers. The server picks the
    # window and holds the lease, so these take no --since/--until/--count: a
    # run imports exactly one server-issued window and advances the checkpoint
    # only when every row of it uploads.
    sync_langfuse_parser = sync_subparsers.add_parser(
        "langfuse",
        description=(
            "Sync Langfuse GENERATION observations to metergraph in server-"
            "coordinated cron mode: pull one logical time window (at most one "
            "hour) from the Langfuse Observations API and push it, with "
            "acquire/resume/complete owned by the metergraph import-sync "
            "server. Safe to run from cron, idempotent, no local checkpoint "
            "files. Requires LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY and "
            "METERGRAPH_APP_TOKEN. --source-scope names the Langfuse project "
            "on the metergraph side (default: the public key, which identifies "
            "the project and is not a secret). --initial-since seeds only the "
            "first run. Observation content is uploaded with no opt-out."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        help="Sync Langfuse generations to metergraph (server-coordinated cron mode)",
    )
    _add_sync_common(sync_langfuse_parser, scope_help=(
        "Identifier for this Langfuse project on the metergraph side "
        "(default: the Langfuse public key, which is not a secret)."
    ))
    sync_langfuse_parser.add_argument(
        "--trace-name", action="append", default=None, metavar="TRACE_NAME",
        help="Only sync generations whose trace has this name. Repeatable (OR).",
    )
    sync_langfuse_parser.add_argument(
        "--tag", action="append", default=None, metavar="TAG",
        help="Only sync generations whose trace carries this tag. Repeatable (AND).",
    )
    sync_langfuse_parser.add_argument(
        "--environment", default=None,
        help="Only sync generations from this Langfuse environment.",
    )
    sync_langfuse_parser.add_argument(
        "--base-url", default=None,
        help=(
            "Langfuse API base URL. (default: $LANGFUSE_BASE_URL if set, else "
            f"{DEFAULT_LANGFUSE_HOST})"
        ),
    )
    sync_langfuse_parser.add_argument(
        "--langfuse-public-key", default=None, metavar="KEY",
        help="Langfuse public key. Overrides $LANGFUSE_PUBLIC_KEY / .env if given.",
    )
    sync_langfuse_parser.add_argument(
        "--langfuse-secret-key", default=None, metavar="KEY",
        help="Langfuse secret key. Overrides $LANGFUSE_SECRET_KEY / .env if given.",
    )

    sync_braintrust_parser = sync_subparsers.add_parser(
        "braintrust",
        description=(
            "Sync Braintrust LLM spans (span_attributes.type = 'llm') to "
            "metergraph in server-coordinated cron mode: pull one logical time "
            "window (at most one hour) from POST /btql and push it, with "
            "acquire/resume/complete owned by the metergraph import-sync "
            "server. Safe to run from cron, idempotent, no local checkpoint "
            "files. Requires BRAINTRUST_API_KEY and METERGRAPH_APP_TOKEN. "
            "--source-scope defaults to the --project list. --initial-since "
            "seeds only the first run. Span content is uploaded with no opt-out."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        help="Sync Braintrust LLM spans to metergraph (server-coordinated cron mode)",
    )
    _add_sync_common(sync_braintrust_parser, scope_help=(
        "Identifier for this Braintrust project set on the metergraph side "
        "(default: the --project values joined with commas)."
    ))
    sync_braintrust_parser.add_argument(
        "--project", action="append", required=True, metavar="PROJECT",
        help="Braintrust project, by name or id. Repeatable. Required.",
    )
    sync_braintrust_parser.add_argument(
        "--base-url", default=None,
        help=(
            "Braintrust API base URL. (default: $BRAINTRUST_BASE_URL if set, "
            "else the US data plane)"
        ),
    )
    sync_braintrust_parser.add_argument(
        "--braintrust-api-key", default=None, metavar="KEY",
        help="Braintrust API key. Overrides $BRAINTRUST_API_KEY / .env if given.",
    )

    sync_phoenix_parser = sync_subparsers.add_parser(
        "phoenix",
        description=(
            "Sync Arize Phoenix LLM spans (span_kind = LLM) to metergraph in "
            "server-coordinated cron mode: pull one logical time window (at "
            "most one hour) from GET /v1/projects/{project}/spans and push it, "
            "with acquire/resume/complete owned by the metergraph import-sync "
            "server. Safe to run from cron, idempotent, no local checkpoint "
            "files. Requires METERGRAPH_APP_TOKEN; a local Phoenix needs no "
            "credential. --source-scope defaults to the --project list. "
            "--initial-since seeds only the first run. Span content is "
            "uploaded with no opt-out."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        help="Sync Phoenix LLM spans to metergraph (server-coordinated cron mode)",
    )
    _add_sync_common(sync_phoenix_parser, scope_help=(
        "Identifier for this Phoenix project set on the metergraph side "
        "(default: the --project values joined with commas)."
    ))
    sync_phoenix_parser.add_argument(
        "--project", action="append", required=True, metavar="PROJECT",
        help="Phoenix project, by name or id. Repeatable. Required.",
    )
    sync_phoenix_parser.add_argument(
        "--name", action="append", default=None, metavar="SPAN_NAME",
        help="Only sync spans with this name. Repeatable (OR).",
    )
    sync_phoenix_parser.add_argument(
        "--base-url", default=None,
        help=(
            "Phoenix server base URL. (default: $PHOENIX_BASE_URL if set, else "
            f"{DEFAULT_PHOENIX_URL})"
        ),
    )
    sync_phoenix_parser.add_argument(
        "--phoenix-api-key", default=None, metavar="KEY",
        help=(
            "Phoenix API key, only when the server has authentication enabled. "
            "Overrides $PHOENIX_API_KEY / .env if given."
        ),
    )

    demo_parser = subparsers.add_parser(
        "demo", help="Run 1-2 demo conversations with store=True"
    )
    demo_subparsers = demo_parser.add_subparsers(dest="provider", required=True)
    demo_openai_parser = demo_subparsers.add_parser("openai")
    demo_openai_parser.add_argument("--model", default="gpt-4o-mini")
    demo_openai_parser.add_argument("--env-file", default=".env")

    push_parser = subparsers.add_parser(
        "push", help="Push a local JSONL file of traces to metergraph"
    )
    push_parser.add_argument("file")
    push_parser.add_argument("--env-file", default=".env")

    return parser


def _add_sync_common(parser: argparse.ArgumentParser, *, scope_help: str) -> None:
    """Flags shared by the server-coordinated sync commands."""
    parser.add_argument("--source-scope", default=None, help=scope_help)
    parser.add_argument(
        "--initial-since",
        default=None,
        help=(
            "Aware ISO 8601 timestamp seeding the first sync window. Required "
            "only on the very first run; the server ignores it once state "
            "exists, so cron may pass it every run."
        ),
    )
    parser.add_argument(
        "--max-window-seconds",
        type=int,
        default=None,
        help="Maximum logical window length in seconds (1-3600). (default: 3600)",
    )
    parser.add_argument(
        "--route",
        default=None,
        help="Override the metergraph route field for every synced row.",
    )
    parser.add_argument(
        "--allow-skipped",
        action="store_true",
        help=(
            "Complete a window even if the provider skipped rows it could not "
            "normalize. By default such a window is left pending, because a "
            "skipped row cannot be recovered once the checkpoint advances."
        ),
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to a .env file to load credentials from. (default: .env)",
    )


def _config_error(exc: ConfigError) -> int:
    print(f"Error: {exc}", file=sys.stderr)
    return 1


def _os_error(exc: OSError | UnicodeDecodeError) -> int:
    print(f"Error: {exc}", file=sys.stderr)
    return 1


def _not_implemented(provider: str) -> int:
    print(
        f"Error: pulling from {provider} is not implemented in this version.",
        file=sys.stderr,
    )
    return 1


def _resolve_langfuse_credentials(args: argparse.Namespace) -> tuple[str, str]:
    # Load the selected --env-file unconditionally, even when CLI credential
    # flags are given below and its own credentials go unused — other
    # settings (e.g. LANGFUSE_BASE_URL) may still live only in that file, not
    # the real process environment, and must still resolve.
    load_dotenv(args.env_file, override=True)
    if args.langfuse_public_key and args.langfuse_secret_key:
        return args.langfuse_public_key, args.langfuse_secret_key
    creds = require_credentials("langfuse", args.env_file)
    return creds["LANGFUSE_PUBLIC_KEY"], creds["LANGFUSE_SECRET_KEY"]


def _resolve_braintrust_credential(args: argparse.Namespace) -> str:
    # Load the selected --env-file unconditionally, even when --braintrust-api-key
    # is given and the file's own credential goes unused — other settings (e.g.
    # BRAINTRUST_BASE_URL) may live only in that file, not the real process
    # environment, and must still resolve.
    load_dotenv(args.env_file, override=True)
    if args.braintrust_api_key:
        return args.braintrust_api_key
    return require_credentials("braintrust", args.env_file)["BRAINTRUST_API_KEY"]


def _cleanup_temp_file(tmp_path: str) -> None:
    try:
        os.remove(tmp_path)
    except OSError:
        pass


def _run_sync_portkey(args: argparse.Namespace) -> int:
    # A local EXPORT_FILE selects manual mode, which never contacts Portkey, so the
    # API-cron-only flags have no effect here. Reject them up front (before touching
    # credentials) instead of silently ignoring config the caller clearly intended.
    api_only = [
        flag
        for flag, given in (
            ("--source-scope", args.source_scope is not None),
            ("--initial-since", args.initial_since is not None),
            ("--max-window-seconds", args.max_window_seconds is not None),
        )
        if given
    ]
    if api_only:
        print(
            f"Error: {', '.join(api_only)} "
            f"{'is' if len(api_only) == 1 else 'are'} only valid in Portkey API cron "
            "mode (omit EXPORT_FILE); they have no effect in manual mode.",
            file=sys.stderr,
        )
        return 1
    try:
        push_creds = require_credentials("push", args.env_file)
    except ConfigError as exc:
        return _config_error(exc)

    try:
        if args.output:
            tmp_dir = os.path.dirname(args.output) or "."
            fd, tmp_path = tempfile.mkstemp(
                dir=tmp_dir, prefix=".portkey-sync-", suffix=".tmp"
            )
        else:
            fd, tmp_path = tempfile.mkstemp(prefix="portkey-sync-", suffix=".jsonl")
        os.close(fd)
    except OSError as exc:
        return _os_error(exc)

    try:
        converted, skipped = convert_portkey_export(args.export_file, tmp_path)
    except (OSError, UnicodeDecodeError) as exc:
        _cleanup_temp_file(tmp_path)
        return _os_error(exc)

    if args.output:
        try:
            os.replace(tmp_path, args.output)
        except OSError as exc:
            _cleanup_temp_file(tmp_path)
            return _os_error(exc)
        working_path = args.output
    else:
        working_path = tmp_path

    if converted == 0:
        print(f"Converted 0 row(s), skipped {skipped}, pushed 0 row(s), 0 failed.")
        if not args.output:
            _cleanup_temp_file(working_path)
        return 0

    base_url = os.environ.get("METERGRAPH_INGEST_URL")
    try:
        succeeded, failed = push_file(
            working_path, push_creds["METERGRAPH_APP_TOKEN"], base_url=base_url
        )
    except OSError as exc:
        return _os_error(exc)
    finally:
        if not args.output:
            _cleanup_temp_file(working_path)

    print(
        f"Converted {converted} row(s), skipped {skipped}, "
        f"pushed {succeeded} row(s), {failed} failed."
    )
    return 1 if failed else 0


def _validate_initial_since(value: str) -> None:
    # Local, best-effort guard: the window contract requires an aware ISO 8601
    # timestamp. The server is the authority on whether initial_since is needed
    # at all (only on first-run state), so we validate the format when given but
    # never require it here. Not a secret, so echoing the bad value is safe.
    try:
        parsed = datetime.fromisoformat(normalize_utc_designator(value))
    except ValueError as exc:
        raise ConfigError(
            f"--initial-since must be an ISO 8601 timestamp, got {value!r}: {exc}"
        ) from exc
    if parsed.tzinfo is None:
        raise ConfigError(
            f"--initial-since must be timezone-aware (include an offset such as "
            f"+00:00), got naive value {value!r}."
        )


def _run_sync_portkey_api(args: argparse.Namespace) -> int:
    if args.output is not None:
        print(
            "Error: --output is only valid with a local EXPORT_FILE (manual mode).",
            file=sys.stderr,
        )
        return 1
    try:
        portkey_creds = require_credentials("portkey", args.env_file)
        push_creds = require_credentials("push", args.env_file)
    except ConfigError as exc:
        return _config_error(exc)
    # require_credentials() has now loaded the env file; non-secret config can be read.
    source_scope = args.source_scope or os.environ.get("PORTKEY_WORKSPACE")
    if not source_scope:
        return _config_error(
            ConfigError(
                "source scope not set. Pass --source-scope or set PORTKEY_WORKSPACE "
                "(the Portkey workspace id; not a secret)."
            )
        )
    if args.initial_since is not None:
        try:
            _validate_initial_since(args.initial_since)
        except ConfigError as exc:
            return _config_error(exc)
    max_window = args.max_window_seconds if args.max_window_seconds is not None else 3600
    if max_window <= 0 or max_window > 3600:
        return _config_error(
            ConfigError("--max-window-seconds must be between 1 and 3600.")
        )
    ingest_base = os.environ.get("METERGRAPH_INGEST_URL")
    portkey_base = os.environ.get("PORTKEY_BASE_URL")
    mg_client = MeterGraphSyncClient(
        ingest_base or DEFAULT_INGEST_URL, push_creds["METERGRAPH_APP_TOKEN"]
    )
    pk_kwargs = {"workspace": source_scope}
    if portkey_base:
        pk_kwargs["base_url"] = portkey_base
    pk_client = PortkeyExportClient(portkey_creds["PORTKEY_API_KEY"], **pk_kwargs)
    try:
        outcome = run_portkey_sync(
            mg_client=mg_client,
            pk_client=pk_client,
            source_scope=source_scope,
            initial_since=args.initial_since,
            max_window_seconds=max_window,
            push_token=push_creds["METERGRAPH_APP_TOKEN"],
            ingest_base_url=ingest_base,
        )
    except (MeterGraphSyncError, PortkeyExportError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(outcome.detail)
    return outcome.exit_code


def _run_pull_langfuse(args: argparse.Namespace) -> int:
    try:
        public_key, secret_key = _resolve_langfuse_credentials(args)
    except ConfigError as exc:
        return _config_error(exc)
    base_url = (
        args.base_url or os.environ.get("LANGFUSE_BASE_URL") or DEFAULT_LANGFUSE_HOST
    )
    until = args.until or datetime.now(timezone.utc).isoformat()
    try:
        imported, skipped = pull_langfuse(
            base_url=base_url,
            public_key=public_key,
            secret_key=secret_key,
            count=args.count,
            since=args.since,
            until=until,
            trace_names=args.trace_name or [],
            tags=args.tag or [],
            environment=args.environment,
            route=args.route,
            output_path=args.output,
        )
    except (LangfuseAPIError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Imported {imported} trace(s), skipped {skipped}, to {args.output}")
    return 0


def _run_pull_braintrust(args: argparse.Namespace) -> int:
    try:
        api_key = _resolve_braintrust_credential(args)
    except ConfigError as exc:
        return _config_error(exc)
    base_url = (
        args.base_url
        or os.environ.get("BRAINTRUST_BASE_URL")
        or DEFAULT_BRAINTRUST_URL
    )
    until = args.until or datetime.now(timezone.utc).isoformat()
    try:
        imported, skipped = pull_braintrust(
            base_url=base_url,
            api_key=api_key,
            projects=args.project,
            count=args.count,
            since=args.since,
            until=until,
            route=args.route,
            output_path=args.output,
        )
    except (BraintrustAPIError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Imported {imported} span(s), skipped {skipped}, to {args.output}")
    return 0


def _sync_window_settings(args: argparse.Namespace) -> tuple[int, str | None]:
    """Validate the shared sync flags; returns (max_window_seconds, initial_since)."""
    if args.initial_since is not None:
        _validate_initial_since(args.initial_since)
    max_window = args.max_window_seconds if args.max_window_seconds is not None else 3600
    if max_window <= 0 or max_window > 3600:
        raise ConfigError("--max-window-seconds must be between 1 and 3600.")
    return max_window, args.initial_since


def _run_sync_pull(args: argparse.Namespace, *, source: str, source_scope: str,
                   pull_window, provider_errors: tuple[type[Exception], ...],
                   push_token: str) -> int:
    """Drive one server-coordinated window for a cursor-paged provider."""
    ingest_base = os.environ.get("METERGRAPH_INGEST_URL")
    mg_client = MeterGraphSyncClient(ingest_base or DEFAULT_INGEST_URL, push_token)
    try:
        max_window, initial_since = _sync_window_settings(args)
    except ConfigError as exc:
        return _config_error(exc)
    try:
        outcome = run_pull_sync(
            mg_client=mg_client,
            source=source,
            source_scope=source_scope,
            pull_window=pull_window,
            initial_since=initial_since,
            max_window_seconds=max_window,
            push_token=push_token,
            ingest_base_url=ingest_base,
            provider_errors=provider_errors,
            allow_skipped=args.allow_skipped,
        )
    except (MeterGraphSyncError, OSError, *provider_errors) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(outcome.detail)
    return outcome.exit_code


def _run_sync_langfuse(args: argparse.Namespace) -> int:
    try:
        public_key, secret_key = _resolve_langfuse_credentials(args)
        push_creds = require_credentials("push", args.env_file)
    except ConfigError as exc:
        return _config_error(exc)
    base_url = (
        args.base_url or os.environ.get("LANGFUSE_BASE_URL") or DEFAULT_LANGFUSE_HOST
    )
    # The public key identifies the Langfuse project and is designed to ship in
    # client-side code, so it is a safe default scope; the secret never is.
    source_scope = args.source_scope or public_key

    def pull_window(*, window_start, window_end, output_path, import_context, on_progress):
        return pull_langfuse(
            base_url=base_url,
            public_key=public_key,
            secret_key=secret_key,
            count=UNBOUNDED_COUNT,
            since=window_start,
            until=window_end,
            trace_names=args.trace_name or [],
            tags=args.tag or [],
            environment=args.environment,
            route=args.route,
            output_path=output_path,
            import_context=import_context,
            on_progress=on_progress,
        )

    return _run_sync_pull(
        args, source="langfuse", source_scope=source_scope, pull_window=pull_window,
        provider_errors=(LangfuseAPIError,), push_token=push_creds["METERGRAPH_APP_TOKEN"],
    )


def _run_sync_braintrust(args: argparse.Namespace) -> int:
    try:
        api_key = _resolve_braintrust_credential(args)
        push_creds = require_credentials("push", args.env_file)
    except ConfigError as exc:
        return _config_error(exc)
    base_url = (
        args.base_url
        or os.environ.get("BRAINTRUST_BASE_URL")
        or DEFAULT_BRAINTRUST_URL
    )
    source_scope = args.source_scope or ",".join(args.project)

    def pull_window(*, window_start, window_end, output_path, import_context, on_progress):
        return pull_braintrust(
            base_url=base_url,
            api_key=api_key,
            projects=args.project,
            count=UNBOUNDED_COUNT,
            since=window_start,
            until=window_end,
            route=args.route,
            output_path=output_path,
            import_context=import_context,
            on_progress=on_progress,
        )

    return _run_sync_pull(
        args, source="braintrust", source_scope=source_scope, pull_window=pull_window,
        provider_errors=(BraintrustAPIError,), push_token=push_creds["METERGRAPH_APP_TOKEN"],
    )


def _run_sync_phoenix(args: argparse.Namespace) -> int:
    try:
        push_creds = require_credentials("push", args.env_file)
    except ConfigError as exc:
        return _config_error(exc)
    # require_credentials() has loaded the env file; optional settings follow.
    api_key = args.phoenix_api_key or os.environ.get("PHOENIX_API_KEY") or None
    base_url = (
        args.base_url or os.environ.get("PHOENIX_BASE_URL") or DEFAULT_PHOENIX_URL
    )
    source_scope = args.source_scope or ",".join(args.project)

    def pull_window(*, window_start, window_end, output_path, import_context, on_progress):
        return pull_phoenix(
            base_url=base_url,
            api_key=api_key,
            projects=args.project,
            count=UNBOUNDED_COUNT,
            since=window_start,
            until=window_end,
            names=args.name or [],
            route=args.route,
            output_path=output_path,
            import_context=import_context,
            on_progress=on_progress,
        )

    return _run_sync_pull(
        args, source="phoenix", source_scope=source_scope, pull_window=pull_window,
        provider_errors=(PhoenixAPIError,), push_token=push_creds["METERGRAPH_APP_TOKEN"],
    )


def _run_pull_phoenix(args: argparse.Namespace) -> int:
    # Phoenix needs no credential by default (a local server has auth off), so
    # the .env file is loaded for its optional settings rather than through
    # require_credentials, which would fail on a missing key.
    load_dotenv(args.env_file, override=True)
    api_key = args.phoenix_api_key or os.environ.get("PHOENIX_API_KEY") or None
    base_url = (
        args.base_url or os.environ.get("PHOENIX_BASE_URL") or DEFAULT_PHOENIX_URL
    )
    until = args.until or datetime.now(timezone.utc).isoformat()
    try:
        imported, skipped = pull_phoenix(
            base_url=base_url,
            api_key=api_key,
            projects=args.project,
            count=args.count,
            since=args.since,
            until=until,
            names=args.name or [],
            route=args.route,
            output_path=args.output,
        )
    except (PhoenixAPIError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Imported {imported} span(s), skipped {skipped}, to {args.output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "pull" and args.provider == "anthropic":
        try:
            require_credentials(args.provider, args.env_file)
        except ConfigError as exc:
            return _config_error(exc)
        return _not_implemented(args.provider)

    if args.command == "pull" and args.provider == "langfuse":
        return _run_pull_langfuse(args)

    if args.command == "pull" and args.provider == "braintrust":
        return _run_pull_braintrust(args)

    if args.command == "pull" and args.provider == "phoenix":
        return _run_pull_phoenix(args)

    if args.command == "sync" and args.provider == "langfuse":
        return _run_sync_langfuse(args)

    if args.command == "sync" and args.provider == "braintrust":
        return _run_sync_braintrust(args)

    if args.command == "sync" and args.provider == "phoenix":
        return _run_sync_phoenix(args)

    if args.command == "pull" and args.provider == "openai":
        try:
            creds = require_credentials("openai", args.env_file)
        except ConfigError as exc:
            return _config_error(exc)
        client = OpenAI(api_key=creds["OPENAI_API_KEY"])
        try:
            written = pull_openai(
                client,
                args.count,
                args.output,
                route=args.route,
                include_content=args.include_content,
                echo_stdout=args.stdout,
            )
        except OSError as exc:
            return _os_error(exc)
        if written == 0:
            print("No stored completions found. Try `metergraphrelay demo openai` first.")
        else:
            print(f"Wrote {written} trace(s) to {args.output}")
        return 0

    if args.command == "sync" and args.provider == "openai":
        try:
            openai_creds = require_credentials("openai", args.env_file)
            push_creds = require_credentials("push", args.env_file)
        except ConfigError as exc:
            return _config_error(exc)
        client = OpenAI(api_key=openai_creds["OPENAI_API_KEY"])
        try:
            written = pull_openai(
                client,
                args.count,
                args.output,
                route=args.route,
                include_content=args.include_content,
                echo_stdout=args.stdout,
            )
        except OSError as exc:
            return _os_error(exc)
        if written == 0:
            print("No stored completions found. Try `metergraphrelay demo openai` first.")
            return 0
        print(f"Wrote {written} trace(s) to {args.output}")
        base_url = os.environ.get("METERGRAPH_INGEST_URL")
        try:
            succeeded, failed = push_file(
                args.output, push_creds["METERGRAPH_APP_TOKEN"], base_url=base_url
            )
        except OSError as exc:
            return _os_error(exc)
        if failed:
            print(f"Pushed {succeeded} row(s), {failed} failed.", file=sys.stderr)
            return 1
        print(f"Pushed {succeeded} row(s) to metergraph.")
        return 0

    if args.command == "sync" and args.provider == "portkey":
        if args.export_file is not None:
            return _run_sync_portkey(args)
        return _run_sync_portkey_api(args)

    if args.command == "demo" and args.provider == "openai":
        try:
            creds = require_credentials("openai", args.env_file)
        except ConfigError as exc:
            return _config_error(exc)
        client = OpenAI(api_key=creds["OPENAI_API_KEY"])
        run_demo(client, model=args.model)
        return 0

    if args.command == "push":
        try:
            creds = require_credentials("push", args.env_file)
        except ConfigError as exc:
            return _config_error(exc)
        base_url = os.environ.get("METERGRAPH_INGEST_URL")
        try:
            succeeded, failed = push_file(
                args.file, creds["METERGRAPH_APP_TOKEN"], base_url=base_url
            )
        except OSError as exc:
            return _os_error(exc)
        if failed:
            print(f"Pushed {succeeded} row(s), {failed} failed.", file=sys.stderr)
            return 1
        print(f"Pushed {succeeded} row(s) to metergraph.")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
