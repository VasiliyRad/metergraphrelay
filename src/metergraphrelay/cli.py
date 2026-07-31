from __future__ import annotations

import argparse
import sys

from openai import OpenAI

from .config import ConfigError, load_api_key
from .demo import run_demo
from .export import export_traces


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openai-exporter")
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo_parser = subparsers.add_parser(
        "demo", help="Run 1-2 demo conversations with store=True"
    )
    demo_parser.add_argument("--model", default="gpt-4o-mini")
    demo_parser.add_argument("--env-file", default=".env")

    export_parser = subparsers.add_parser(
        "export", help="Export N stored chat completions as JSONL traces"
    )
    export_parser.add_argument("-n", "--count", type=int, default=10)
    export_parser.add_argument("--output", default="traces.jsonl")
    export_parser.add_argument("--stdout", action="store_true")
    export_parser.add_argument("--env-file", default=".env")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        api_key = load_api_key(args.env_file)
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    client = OpenAI(api_key=api_key)

    if args.command == "demo":
        run_demo(client, model=args.model)
        return 0

    written = export_traces(client, args.count, args.output, echo_stdout=args.stdout)
    if written == 0:
        print("No stored completions found. Try `openai-exporter demo` first.")
    else:
        print(f"Wrote {written} trace(s) to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
