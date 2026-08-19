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
from .providers.langfuse import DEFAULT_LANGFUSE_HOST, LangfuseAPIError, pull_langfuse
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
