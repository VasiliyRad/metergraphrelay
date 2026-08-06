from __future__ import annotations

import argparse
import os
import sys

from openai import OpenAI

from .config import ConfigError, require_credentials
from .demo import run_demo
from .providers.openai import pull_openai
from .push import push_file


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

    pull_langfuse_parser = pull_subparsers.add_parser("langfuse")
    pull_langfuse_parser.add_argument("--output", default="./traces.jsonl")
    pull_langfuse_parser.add_argument("--env-file", default=".env")

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


def _os_error(exc: OSError) -> int:
    print(f"Error: {exc}", file=sys.stderr)
    return 1


def _not_implemented(provider: str) -> int:
    print(
        f"Error: pulling from {provider} is not implemented in this version.",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "pull" and args.provider in {"anthropic", "langfuse"}:
        try:
            require_credentials(args.provider, args.env_file)
        except ConfigError as exc:
            return _config_error(exc)
        return _not_implemented(args.provider)

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
