# openai-exporter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a pip-installable Python CLI, `openai-exporter`, with a `demo` subcommand that runs 1-2 stored chat completions and an `export` subcommand that lists N stored chat completions from OpenAI and writes them as normalized JSONL trace records.

**Architecture:** A small `openai_exporter` package: `config.py` resolves the API key from a `.env` file, `export.py` normalizes and writes stored-completion data to JSONL, `demo.py` runs the canned demo conversations, and `cli.py` wires both into `argparse` subcommands behind a single `main()` entry point.

**Tech Stack:** Python 3.10+, `openai` SDK, `python-dotenv`, stdlib `argparse`, `pytest` for tests (mocking the OpenAI client — no real API calls in tests).

## Global Constraints

- Python 3.10+ only.
- Dependencies limited to `openai` and `python-dotenv`; CLI built with stdlib `argparse` (no Typer/Click).
- Config file is `.env` (default path `./.env`, overridable via `--env-file`) holding only `OPENAI_API_KEY`.
- Output format is JSON Lines only, one trace object per line; default path `./traces.jsonl`, overridable via `--output`.
- `export` uses a single `chat.completions.list(order="desc", limit=N)` call — no pagination beyond that.
- No retry/backoff logic anywhere; OpenAI SDK errors surface directly.
- `export` scope is all stored completions visible to the API key (not filtered to this tool's own demo data).
- Package installs via `pyproject.toml` with a `console_scripts` entry point (`openai-exporter`).
- Tests mock the OpenAI client; no real network calls in the test suite.

---

### Task 1: Project scaffolding and packaging

**Files:**
- Create: `pyproject.toml`
- Create: `.env.example`
- Create: `README.md`
- Create: `src/openai_exporter/__init__.py`
- Create: `.gitignore`

**Interfaces:**
- Produces: an installable `openai_exporter` package (empty for now) with a declared but not-yet-existing console script `openai-exporter = openai_exporter.cli:main`, and a `dev` extra providing `pytest`. Later tasks add the modules this imports.

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "openai-exporter"
version = "0.1.0"
description = "Export OpenAI stored chat completions as trace records, with a demo mode to generate sample data."
requires-python = ">=3.10"
dependencies = [
    "openai>=1.0",
    "python-dotenv>=1.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0"]

[project.scripts]
openai-exporter = "openai_exporter.cli:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 2: Create `.env.example`**

```
OPENAI_API_KEY=sk-your-key-here
```

- [ ] **Step 3: Create `.gitignore`**

```
.env
*.jsonl
__pycache__/
*.egg-info/
.pytest_cache/
build/
dist/
```

- [ ] **Step 4: Create `README.md`**

```markdown
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
```

- [ ] **Step 5: Create the empty package `src/openai_exporter/__init__.py`**

```python
```

- [ ] **Step 6: Install the package and verify it imports**

Run: `pip install -e ".[dev]"`
Expected: install succeeds with no errors.

Run: `python -c "import openai_exporter; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml .env.example .gitignore README.md src/openai_exporter/__init__.py
git commit -m "chore: scaffold openai-exporter package"
```

---

### Task 2: Config loading

**Files:**
- Create: `src/openai_exporter/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `class ConfigError(Exception)`; `def load_api_key(env_file: str = ".env") -> str` — loads `env_file` via `python-dotenv`, returns the stripped `OPENAI_API_KEY` value from the environment, raises `ConfigError` (message contains the substring `"OPENAI_API_KEY"`) if it's missing or blank.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_config.py`:

```python
import pytest

from openai_exporter.config import ConfigError, load_api_key


def test_load_api_key_reads_value_from_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=sk-test-123\n")

    assert load_api_key(str(env_file)) == "sk-test-123"


def test_load_api_key_raises_when_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("")

    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        load_api_key(str(env_file))


def test_load_api_key_raises_when_blank(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=   \n")

    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        load_api_key(str(env_file))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'openai_exporter.config'`

- [ ] **Step 3: Write the implementation**

Create `src/openai_exporter/config.py`:

```python
from __future__ import annotations

import os

from dotenv import load_dotenv


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


def load_api_key(env_file: str = ".env") -> str:
    load_dotenv(env_file)
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise ConfigError(
            f"OPENAI_API_KEY is not set. Add it to {env_file} "
            "(see .env.example) or export it in your shell."
        )
    return api_key
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_config.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/openai_exporter/config.py tests/test_config.py
git commit -m "feat: load OPENAI_API_KEY from .env"
```

---

### Task 3: Trace record normalization

**Files:**
- Create: `src/openai_exporter/export.py`
- Test: `tests/test_export.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `def normalize_completion(completion: Any, messages: Iterable[Any], error: Exception | None = None) -> dict` — `completion` is an object with `.id`, `.created` (unix timestamp), `.model`, `.usage` (object with `.prompt_tokens`/`.completion_tokens`, or `None`), `.metadata` (dict or `None`); `messages` is an iterable of objects with `.role`/`.content`. Returns a dict matching the trace record shape from the spec. Task 4 calls this function.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_export.py`:

```python
from types import SimpleNamespace

from openai_exporter.export import normalize_completion


def make_completion(**overrides):
    defaults = dict(
        id="chatcmpl-abc123",
        created=1780000000,
        model="gpt-4o-mini",
        metadata={"foo": "bar"},
        usage=SimpleNamespace(prompt_tokens=12, completion_tokens=34),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def make_message(role, content):
    return SimpleNamespace(role=role, content=content)


def test_normalize_completion_builds_expected_row():
    completion = make_completion()
    messages = [make_message("user", "hi"), make_message("assistant", "hello")]

    row = normalize_completion(completion, messages)

    assert row == {
        "id": "chatcmpl-abc123",
        "ts": "2026-05-28T20:26:40+00:00",
        "model": "gpt-4o-mini",
        "provider": "openai",
        "endpoint": "chat.completions",
        "status": "success",
        "input_tokens": 12,
        "output_tokens": 34,
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ],
        "metadata": {"foo": "bar"},
    }


def test_normalize_completion_handles_missing_usage_and_metadata():
    completion = make_completion(usage=None, metadata=None)

    row = normalize_completion(completion, [])

    assert row["input_tokens"] is None
    assert row["output_tokens"] is None
    assert row["metadata"] == {}
    assert row["messages"] == []


def test_normalize_completion_marks_error_status():
    completion = make_completion()

    row = normalize_completion(completion, [make_message("user", "hi")], error=RuntimeError("boom"))

    assert row["status"] == "error"
    assert row["messages"] == []
    assert row["input_tokens"] is None
    assert row["output_tokens"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_export.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'openai_exporter.export'`

- [ ] **Step 3: Write the implementation**

Create `src/openai_exporter/export.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable


def normalize_completion(
    completion: Any, messages: Iterable[Any], error: Exception | None = None
) -> dict:
    usage = getattr(completion, "usage", None)
    metadata = getattr(completion, "metadata", None) or {}
    ts = datetime.fromtimestamp(completion.created, tz=timezone.utc).isoformat()

    if error is not None:
        return {
            "id": completion.id,
            "ts": ts,
            "model": completion.model,
            "provider": "openai",
            "endpoint": "chat.completions",
            "status": "error",
            "input_tokens": None,
            "output_tokens": None,
            "messages": [],
            "metadata": metadata,
        }

    return {
        "id": completion.id,
        "ts": ts,
        "model": completion.model,
        "provider": "openai",
        "endpoint": "chat.completions",
        "status": "success",
        "input_tokens": getattr(usage, "prompt_tokens", None) if usage else None,
        "output_tokens": getattr(usage, "completion_tokens", None) if usage else None,
        "messages": [
            {"role": message.role, "content": message.content} for message in messages
        ],
        "metadata": metadata,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_export.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/openai_exporter/export.py tests/test_export.py
git commit -m "feat: normalize stored completions into trace records"
```

---

### Task 4: Export orchestration (list, fetch, write JSONL)

**Files:**
- Modify: `src/openai_exporter/export.py`
- Modify: `tests/test_export.py`

**Interfaces:**
- Consumes: `normalize_completion(completion, messages, error=None) -> dict` from Task 3.
- Produces: `def export_traces(client: Any, count: int, output_path: str, *, echo_stdout: bool = False) -> int` — calls `client.chat.completions.list(order="desc", limit=count)`, then for each completion calls `client.chat.completions.messages.list(completion.id)`, normalizes, writes one JSON line per completion to `output_path`, optionally prints each line, and returns the number of lines written. Task 6 (`cli.py`) calls this function.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_export.py`:

```python
import json

from unittest.mock import MagicMock

from openai_exporter.export import export_traces


def test_export_traces_writes_jsonl_for_each_completion(tmp_path):
    client = MagicMock()
    completions = [
        make_completion(id="chatcmpl-1", created=1780000000),
        make_completion(id="chatcmpl-2", created=1780000100),
    ]
    client.chat.completions.list.return_value = completions
    messages_by_id = {
        "chatcmpl-1": [make_message("user", "hi")],
        "chatcmpl-2": [make_message("user", "yo")],
    }
    client.chat.completions.messages.list.side_effect = (
        lambda completion_id: messages_by_id[completion_id]
    )
    output_path = tmp_path / "traces.jsonl"

    written = export_traces(client, count=2, output_path=str(output_path))

    client.chat.completions.list.assert_called_once_with(order="desc", limit=2)
    assert written == 2
    lines = output_path.read_text().splitlines()
    assert len(lines) == 2
    row1 = json.loads(lines[0])
    assert row1["id"] == "chatcmpl-1"
    assert row1["messages"] == [{"role": "user", "content": "hi"}]


def test_export_traces_empty_list_writes_empty_file(tmp_path):
    client = MagicMock()
    client.chat.completions.list.return_value = []
    output_path = tmp_path / "traces.jsonl"

    written = export_traces(client, count=5, output_path=str(output_path))

    assert written == 0
    assert output_path.read_text() == ""


def test_export_traces_marks_message_fetch_failure_as_error(tmp_path):
    client = MagicMock()
    completion = make_completion(id="chatcmpl-1", created=1780000000)
    client.chat.completions.list.return_value = [completion]
    client.chat.completions.messages.list.side_effect = RuntimeError("boom")
    output_path = tmp_path / "traces.jsonl"

    written = export_traces(client, count=1, output_path=str(output_path))

    assert written == 1
    row = json.loads(output_path.read_text().splitlines()[0])
    assert row["status"] == "error"
    assert row["messages"] == []


def test_export_traces_echoes_to_stdout_when_enabled(tmp_path, capsys):
    client = MagicMock()
    completion = make_completion(id="chatcmpl-1", created=1780000000)
    client.chat.completions.list.return_value = [completion]
    client.chat.completions.messages.list.return_value = [make_message("user", "hi")]
    output_path = tmp_path / "traces.jsonl"

    export_traces(client, count=1, output_path=str(output_path), echo_stdout=True)

    captured = capsys.readouterr()
    assert "chatcmpl-1" in captured.out
```

Note: `make_completion` here is called with `id=` / `created=` keyword overrides, matching the `**overrides` signature already defined earlier in the file from Task 3.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_export.py -v`
Expected: FAIL — `ImportError: cannot import name 'export_traces'`

- [ ] **Step 3: Write the implementation**

Add to `src/openai_exporter/export.py` (below `normalize_completion`, add `import json` to the existing imports at the top):

```python
import json
```

```python
def export_traces(
    client: Any, count: int, output_path: str, *, echo_stdout: bool = False
) -> int:
    completions = client.chat.completions.list(order="desc", limit=count)
    written = 0
    with open(output_path, "w") as f:
        for completion in completions:
            try:
                messages = client.chat.completions.messages.list(completion.id)
                row = normalize_completion(completion, messages)
            except Exception as exc:
                row = normalize_completion(completion, [], error=exc)
            line = json.dumps(row)
            f.write(line + "\n")
            if echo_stdout:
                print(line)
            written += 1
    return written
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_export.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/openai_exporter/export.py tests/test_export.py
git commit -m "feat: list and export stored completions as JSONL"
```

---

### Task 5: Demo conversations

**Files:**
- Create: `src/openai_exporter/demo.py`
- Test: `tests/test_demo.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `DEMO_PROMPTS: list[str]` (2 fixed prompts); `def run_demo(client: Any, *, model: str = "gpt-4o-mini") -> list[dict]` — calls `client.chat.completions.create(model=model, store=True, messages=[{"role": "user", "content": prompt}])` once per prompt, prints each prompt/reply pair, and returns `[{"prompt": ..., "reply": ...}, ...]`. Task 6 (`cli.py`) calls this function.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_demo.py`:

```python
from types import SimpleNamespace
from unittest.mock import MagicMock

from openai_exporter.demo import DEMO_PROMPTS, run_demo


def make_completion(reply_text):
    message = SimpleNamespace(content=reply_text)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice])


def test_run_demo_sends_each_prompt_with_store_true():
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        make_completion("Hello there."),
        make_completion("4"),
    ]

    results = run_demo(client, model="gpt-4o-mini")

    assert client.chat.completions.create.call_count == len(DEMO_PROMPTS)
    for call, prompt in zip(client.chat.completions.create.call_args_list, DEMO_PROMPTS):
        kwargs = call.kwargs
        assert kwargs["model"] == "gpt-4o-mini"
        assert kwargs["store"] is True
        assert kwargs["messages"] == [{"role": "user", "content": prompt}]
    assert results == [
        {"prompt": DEMO_PROMPTS[0], "reply": "Hello there."},
        {"prompt": DEMO_PROMPTS[1], "reply": "4"},
    ]


def test_run_demo_prints_prompt_and_reply(capsys):
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        make_completion("Hello there."),
        make_completion("4"),
    ]

    run_demo(client, model="gpt-4o-mini")

    captured = capsys.readouterr()
    assert DEMO_PROMPTS[0] in captured.out
    assert "Hello there." in captured.out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_demo.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'openai_exporter.demo'`

- [ ] **Step 3: Write the implementation**

Create `src/openai_exporter/demo.py`:

```python
from __future__ import annotations

from typing import Any

DEMO_PROMPTS = [
    "Say hello in one sentence.",
    "What's 2+2?",
]


def run_demo(client: Any, *, model: str = "gpt-4o-mini") -> list[dict]:
    results = []
    for prompt in DEMO_PROMPTS:
        completion = client.chat.completions.create(
            model=model,
            store=True,
            messages=[{"role": "user", "content": prompt}],
        )
        reply = completion.choices[0].message.content
        print(f"> {prompt}\n{reply}\n")
        results.append({"prompt": prompt, "reply": reply})
    return results
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_demo.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add src/openai_exporter/demo.py tests/test_demo.py
git commit -m "feat: run demo conversations with store=True"
```

---

### Task 6: CLI wiring

**Files:**
- Create: `src/openai_exporter/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `ConfigError`, `load_api_key(env_file: str) -> str` (Task 2); `export_traces(client, count, output_path, *, echo_stdout=False) -> int` (Task 4); `run_demo(client, *, model="gpt-4o-mini") -> list[dict]` (Task 5); `openai.OpenAI(api_key=...)`.
- Produces: `def main(argv: list[str] | None = None) -> int` — the console-script entry point (`openai-exporter = openai_exporter.cli:main`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cli.py`:

```python
from unittest.mock import patch

from openai_exporter.cli import main


def test_main_returns_error_when_api_key_missing(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("")

    exit_code = main(["export", "--env-file", str(env_file)])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "OPENAI_API_KEY" in captured.err


def test_main_demo_dispatches_to_run_demo(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=sk-test\n")

    with patch("openai_exporter.cli.OpenAI") as mock_openai_cls, patch(
        "openai_exporter.cli.run_demo"
    ) as mock_run_demo:
        exit_code = main(["demo", "--env-file", str(env_file), "--model", "gpt-4o-mini"])

    assert exit_code == 0
    mock_run_demo.assert_called_once_with(
        mock_openai_cls.return_value, model="gpt-4o-mini"
    )


def test_main_export_dispatches_to_export_traces(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=sk-test\n")
    output_path = tmp_path / "out.jsonl"

    with patch("openai_exporter.cli.OpenAI") as mock_openai_cls, patch(
        "openai_exporter.cli.export_traces", return_value=3
    ) as mock_export:
        exit_code = main(
            [
                "export",
                "--env-file",
                str(env_file),
                "-n",
                "3",
                "--output",
                str(output_path),
            ]
        )

    assert exit_code == 0
    mock_export.assert_called_once_with(
        mock_openai_cls.return_value, 3, str(output_path), echo_stdout=False
    )


def test_main_export_reports_when_nothing_found(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=sk-test\n")
    output_path = tmp_path / "out.jsonl"

    with patch("openai_exporter.cli.OpenAI"), patch(
        "openai_exporter.cli.export_traces", return_value=0
    ):
        exit_code = main(
            ["export", "--env-file", str(env_file), "--output", str(output_path)]
        )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "openai-exporter demo" in captured.out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'openai_exporter.cli'`

- [ ] **Step 3: Write the implementation**

Create `src/openai_exporter/cli.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cli.py -v`
Expected: 4 passed

- [ ] **Step 5: Run the full test suite**

Run: `pytest -v`
Expected: all tests across `test_config.py`, `test_export.py`, `test_demo.py`, `test_cli.py` pass.

- [ ] **Step 6: Verify the installed console script**

Run: `openai-exporter --help`
Expected: prints usage showing the `demo` and `export` subcommands.

Run: `openai-exporter export --env-file does-not-exist.env`
Expected: prints `Error: OPENAI_API_KEY is not set...` to stderr and exits non-zero.

- [ ] **Step 7: Commit**

```bash
git add src/openai_exporter/cli.py tests/test_cli.py
git commit -m "feat: wire demo and export subcommands into CLI entry point"
```

---

## Self-Review Notes

- **Spec coverage:** `demo` subcommand (Task 5+6), `export` subcommand (Task 3+4+6), `.env`-only config (Task 2), JSONL output with `--output`/`--stdout` (Task 4/6), trace row shape incl. error status (Task 3), empty-list messaging (Task 6), packaging/console script (Task 1), all covered.
- **Placeholder scan:** none found — every step has runnable code.
- **Type consistency:** `normalize_completion(completion, messages, error=None)` signature (Task 3) matches its Task 4 call sites; `export_traces(client, count, output_path, *, echo_stdout=False)` matches Task 6's call; `run_demo(client, *, model="gpt-4o-mini")` matches Task 6's call. Verified consistent throughout.
