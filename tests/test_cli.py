from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from metergraphrelay.cli import build_parser, main
from metergraphrelay.portkey_sync import SyncOutcome
from metergraphrelay.providers.langfuse import LangfuseAPIError


def test_main_pull_openai_missing_credential_returns_error(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("")

    exit_code = main(["pull", "openai", "--env-file", str(env_file)])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "OPENAI_API_KEY" in captured.err


def test_main_pull_openai_dispatches_to_pull_openai(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=sk-test\n")
    output_path = tmp_path / "out.jsonl"

    with patch("metergraphrelay.cli.OpenAI") as mock_openai_cls, patch(
        "metergraphrelay.cli.pull_openai", return_value=3
    ) as mock_pull:
        exit_code = main(
            [
                "pull",
                "openai",
                "--env-file",
                str(env_file),
                "-n",
                "3",
                "--output",
                str(output_path),
                "--include-content",
            ]
        )

    assert exit_code == 0
    mock_pull.assert_called_once_with(
        mock_openai_cls.return_value,
        3,
        str(output_path),
        route="openai/backfill",
        include_content=True,
        echo_stdout=False,
    )


def test_main_pull_openai_custom_route(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=sk-test\n")

    with patch("metergraphrelay.cli.OpenAI") as mock_openai_cls, patch(
        "metergraphrelay.cli.pull_openai", return_value=0
    ) as mock_pull:
        main(
            [
                "pull",
                "openai",
                "--env-file",
                str(env_file),
                "--route",
                "my-app/support-bot",
            ]
        )

    assert mock_pull.call_args.kwargs["route"] == "my-app/support-bot"


def test_main_pull_anthropic_missing_credential_returns_error(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("")

    exit_code = main(["pull", "anthropic", "--env-file", str(env_file)])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "ANTHROPIC_API_KEY" in captured.err


def test_main_pull_anthropic_reports_not_implemented_when_credential_present(
    tmp_path, capsys
):
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=sk-ant-test\n")

    exit_code = main(["pull", "anthropic", "--env-file", str(env_file)])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "not implemented" in captured.err.lower()
    assert "anthropic" in captured.err.lower()


def test_main_pull_langfuse_missing_credential_returns_error(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("LANGFUSE_PUBLIC_KEY=pk-1\n")

    exit_code = main(["pull", "langfuse", "--env-file", str(env_file)])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "LANGFUSE_SECRET_KEY" in captured.err


def test_main_pull_langfuse_dispatches_to_pull_langfuse(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("LANGFUSE_PUBLIC_KEY=pk-1\nLANGFUSE_SECRET_KEY=sk-1\n")
    output_path = tmp_path / "out.jsonl"

    with patch(
        "metergraphrelay.cli.pull_langfuse", return_value=(5, 1)
    ) as mock_pull:
        exit_code = main(
            [
                "pull",
                "langfuse",
                "--env-file",
                str(env_file),
                "-n",
                "5",
                "--output",
                str(output_path),
                "--until",
                "2026-08-07T00:00:00+00:00",
            ]
        )

    assert exit_code == 0
    mock_pull.assert_called_once_with(
        base_url="https://cloud.langfuse.com",
        public_key="pk-1",
        secret_key="sk-1",
        count=5,
        since=None,
        until="2026-08-07T00:00:00+00:00",
        trace_names=[],
        tags=[],
        environment=None,
        route=None,
        output_path=str(output_path),
    )


def test_main_pull_langfuse_credential_flags_override_env(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("")

    with patch(
        "metergraphrelay.cli.pull_langfuse", return_value=(0, 0)
    ) as mock_pull:
        main(
            [
                "pull",
                "langfuse",
                "--env-file",
                str(env_file),
                "--langfuse-public-key",
                "pk-cli",
                "--langfuse-secret-key",
                "sk-cli",
                "--until",
                "2026-08-07T00:00:00+00:00",
            ]
        )

    assert mock_pull.call_args.kwargs["public_key"] == "pk-cli"
    assert mock_pull.call_args.kwargs["secret_key"] == "sk-cli"


def test_main_pull_langfuse_base_url_from_env_file_resolves_with_cli_credential_flags(
    tmp_path,
):
    # LANGFUSE_BASE_URL lives only in the selected --env-file, not the real
    # process environment. Supplying credentials via CLI flags must not skip
    # loading that file, or this value would never be seen.
    env_file = tmp_path / ".env"
    env_file.write_text("LANGFUSE_BASE_URL=https://env-file-host.example.com\n")

    with patch(
        "metergraphrelay.cli.pull_langfuse", return_value=(0, 0)
    ) as mock_pull:
        main(
            [
                "pull",
                "langfuse",
                "--env-file",
                str(env_file),
                "--langfuse-public-key",
                "pk-cli",
                "--langfuse-secret-key",
                "sk-cli",
                "--until",
                "2026-08-07T00:00:00+00:00",
            ]
        )

    assert (
        mock_pull.call_args.kwargs["base_url"] == "https://env-file-host.example.com"
    )


def test_main_pull_langfuse_base_url_flag_takes_precedence_over_env(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LANGFUSE_PUBLIC_KEY=pk-1\nLANGFUSE_SECRET_KEY=sk-1\n"
        "LANGFUSE_BASE_URL=https://env-host.example.com\n"
    )

    with patch(
        "metergraphrelay.cli.pull_langfuse", return_value=(0, 0)
    ) as mock_pull:
        main(
            [
                "pull",
                "langfuse",
                "--env-file",
                str(env_file),
                "--base-url",
                "https://cli-host.example.com",
                "--until",
                "2026-08-07T00:00:00+00:00",
            ]
        )

    assert mock_pull.call_args.kwargs["base_url"] == "https://cli-host.example.com"


def test_main_pull_langfuse_base_url_falls_back_to_langfuse_base_url_env(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LANGFUSE_PUBLIC_KEY=pk-1\nLANGFUSE_SECRET_KEY=sk-1\n"
        "LANGFUSE_BASE_URL=https://env-host.example.com\n"
    )

    with patch(
        "metergraphrelay.cli.pull_langfuse", return_value=(0, 0)
    ) as mock_pull:
        main(
            [
                "pull",
                "langfuse",
                "--env-file",
                str(env_file),
                "--until",
                "2026-08-07T00:00:00+00:00",
            ]
        )

    assert mock_pull.call_args.kwargs["base_url"] == "https://env-host.example.com"


def test_main_pull_langfuse_base_url_defaults_to_langfuse_cloud(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("LANGFUSE_PUBLIC_KEY=pk-1\nLANGFUSE_SECRET_KEY=sk-1\n")

    with patch(
        "metergraphrelay.cli.pull_langfuse", return_value=(0, 0)
    ) as mock_pull:
        main(
            [
                "pull",
                "langfuse",
                "--env-file",
                str(env_file),
                "--until",
                "2026-08-07T00:00:00+00:00",
            ]
        )

    assert mock_pull.call_args.kwargs["base_url"] == "https://cloud.langfuse.com"


def test_main_pull_langfuse_until_defaults_to_command_start_time(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("LANGFUSE_PUBLIC_KEY=pk-1\nLANGFUSE_SECRET_KEY=sk-1\n")

    with patch(
        "metergraphrelay.cli.pull_langfuse", return_value=(0, 0)
    ) as mock_pull:
        main(["pull", "langfuse", "--env-file", str(env_file)])

    until_value = mock_pull.call_args.kwargs["until"]
    assert until_value is not None
    datetime.fromisoformat(until_value)


def test_main_pull_langfuse_repeatable_trace_name_and_tag(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("LANGFUSE_PUBLIC_KEY=pk-1\nLANGFUSE_SECRET_KEY=sk-1\n")

    with patch(
        "metergraphrelay.cli.pull_langfuse", return_value=(0, 0)
    ) as mock_pull:
        main(
            [
                "pull",
                "langfuse",
                "--env-file",
                str(env_file),
                "--trace-name",
                "support-bot",
                "--trace-name",
                "billing-bot",
                "--tag",
                "prod",
                "--tag",
                "tier-1",
                "--until",
                "2026-08-07T00:00:00+00:00",
            ]
        )

    assert mock_pull.call_args.kwargs["trace_names"] == ["support-bot", "billing-bot"]
    assert mock_pull.call_args.kwargs["tags"] == ["prod", "tier-1"]


def test_main_pull_langfuse_default_count_is_100(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("LANGFUSE_PUBLIC_KEY=pk-1\nLANGFUSE_SECRET_KEY=sk-1\n")

    with patch(
        "metergraphrelay.cli.pull_langfuse", return_value=(0, 0)
    ) as mock_pull:
        main(
            [
                "pull",
                "langfuse",
                "--env-file",
                str(env_file),
                "--until",
                "2026-08-07T00:00:00+00:00",
            ]
        )

    assert mock_pull.call_args.kwargs["count"] == 100


def test_main_pull_langfuse_prints_imported_and_skipped_summary(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("LANGFUSE_PUBLIC_KEY=pk-1\nLANGFUSE_SECRET_KEY=sk-1\n")

    with patch("metergraphrelay.cli.pull_langfuse", return_value=(7, 2)):
        exit_code = main(
            [
                "pull",
                "langfuse",
                "--env-file",
                str(env_file),
                "--until",
                "2026-08-07T00:00:00+00:00",
            ]
        )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "7" in captured.out
    assert "2" in captured.out


def test_main_pull_langfuse_api_error_returns_clean_exit_code(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("LANGFUSE_PUBLIC_KEY=pk-1\nLANGFUSE_SECRET_KEY=sk-1\n")

    with patch(
        "metergraphrelay.cli.pull_langfuse",
        side_effect=LangfuseAPIError(
            "Langfuse API request failed: HTTP 400 Bad Request"
        ),
    ):
        exit_code = main(
            [
                "pull",
                "langfuse",
                "--env-file",
                str(env_file),
                "--until",
                "2026-08-07T00:00:00+00:00",
            ]
        )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("Error: ")
    assert "400" in captured.err


def test_main_demo_openai_dispatches_to_run_demo(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=sk-test\n")

    with patch("metergraphrelay.cli.OpenAI") as mock_openai_cls, patch(
        "metergraphrelay.cli.run_demo"
    ) as mock_run_demo:
        exit_code = main(
            ["demo", "openai", "--env-file", str(env_file), "--model", "gpt-4o-mini"]
        )

    assert exit_code == 0
    mock_run_demo.assert_called_once_with(
        mock_openai_cls.return_value, model="gpt-4o-mini"
    )


def test_main_push_missing_credential_returns_error(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("")
    trace_file = tmp_path / "traces.jsonl"
    trace_file.write_text("")

    exit_code = main(["push", str(trace_file), "--env-file", str(env_file)])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "METERGRAPH_APP_TOKEN" in captured.err


def test_main_push_dispatches_to_push_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("METERGRAPH_APP_TOKEN=tok-123\n")
    trace_file = tmp_path / "traces.jsonl"
    trace_file.write_text("")

    with patch(
        "metergraphrelay.cli.push_file", return_value=(2, 0)
    ) as mock_push:
        exit_code = main(["push", str(trace_file), "--env-file", str(env_file)])

    assert exit_code == 0
    mock_push.assert_called_once_with(str(trace_file), "tok-123", base_url=None)


def test_main_push_returns_error_exit_code_when_any_row_fails(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("METERGRAPH_APP_TOKEN=tok-123\n")
    trace_file = tmp_path / "traces.jsonl"
    trace_file.write_text("")

    with patch("metergraphrelay.cli.push_file", return_value=(1, 1)):
        exit_code = main(["push", str(trace_file), "--env-file", str(env_file)])

    assert exit_code == 1


def test_main_push_missing_input_file_returns_clean_error(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("METERGRAPH_APP_TOKEN=tok-123\n")
    missing_file = tmp_path / "nope.jsonl"

    # Real push_file runs against a real missing path: no traceback should escape.
    exit_code = main(["push", str(missing_file), "--env-file", str(env_file)])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("Error: ")
    assert "nope.jsonl" in captured.err
    assert "Traceback" not in captured.err


def test_main_pull_openai_unwritable_output_returns_clean_error(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=sk-test\n")

    with patch("metergraphrelay.cli.OpenAI"), patch(
        "metergraphrelay.cli.pull_openai",
        side_effect=FileNotFoundError(
            2, "No such file or directory", "/no/such/dir/t.jsonl"
        ),
    ):
        exit_code = main(
            [
                "pull",
                "openai",
                "--env-file",
                str(env_file),
                "--output",
                "/no/such/dir/t.jsonl",
            ]
        )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("Error: ")
    assert "/no/such/dir/t.jsonl" in captured.err


def test_main_sync_openai_missing_openai_credential_returns_error(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("METERGRAPH_APP_TOKEN=tok-123\n")

    with patch("metergraphrelay.cli.pull_openai") as mock_pull, patch(
        "metergraphrelay.cli.push_file"
    ) as mock_push:
        exit_code = main(["sync", "openai", "--env-file", str(env_file)])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "OPENAI_API_KEY" in captured.err
    mock_pull.assert_not_called()
    mock_push.assert_not_called()


def test_main_sync_openai_missing_push_credential_returns_error(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=sk-test\n")

    with patch("metergraphrelay.cli.pull_openai") as mock_pull, patch(
        "metergraphrelay.cli.push_file"
    ) as mock_push:
        exit_code = main(["sync", "openai", "--env-file", str(env_file)])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "METERGRAPH_APP_TOKEN" in captured.err
    mock_pull.assert_not_called()
    mock_push.assert_not_called()


def test_main_sync_openai_pulls_then_pushes(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=sk-test\nMETERGRAPH_APP_TOKEN=tok-123\n")
    output_path = tmp_path / "out.jsonl"

    with patch("metergraphrelay.cli.OpenAI") as mock_openai_cls, patch(
        "metergraphrelay.cli.pull_openai", return_value=3
    ) as mock_pull, patch(
        "metergraphrelay.cli.push_file", return_value=(3, 0)
    ) as mock_push:
        exit_code = main(
            [
                "sync",
                "openai",
                "--env-file",
                str(env_file),
                "-n",
                "3",
                "--output",
                str(output_path),
            ]
        )

    assert exit_code == 0
    mock_pull.assert_called_once_with(
        mock_openai_cls.return_value,
        3,
        str(output_path),
        route="openai/backfill",
        include_content=False,
        echo_stdout=False,
    )
    mock_push.assert_called_once_with(str(output_path), "tok-123", base_url=None)


def test_main_sync_openai_reports_when_nothing_to_pull_and_skips_push(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=sk-test\nMETERGRAPH_APP_TOKEN=tok-123\n")

    with patch("metergraphrelay.cli.OpenAI"), patch(
        "metergraphrelay.cli.pull_openai", return_value=0
    ), patch("metergraphrelay.cli.push_file") as mock_push:
        exit_code = main(["sync", "openai", "--env-file", str(env_file)])

    assert exit_code == 0
    mock_push.assert_not_called()


def test_main_sync_openai_returns_error_when_push_fails(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=sk-test\nMETERGRAPH_APP_TOKEN=tok-123\n")

    with patch("metergraphrelay.cli.OpenAI"), patch(
        "metergraphrelay.cli.pull_openai", return_value=2
    ), patch("metergraphrelay.cli.push_file", return_value=(1, 1)):
        exit_code = main(["sync", "openai", "--env-file", str(env_file)])

    assert exit_code == 1


def test_main_push_uses_custom_ingest_url_from_env(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "METERGRAPH_APP_TOKEN=tok-123\n"
        "METERGRAPH_INGEST_URL=http://localhost:8080\n"
    )
    trace_file = tmp_path / "traces.jsonl"
    trace_file.write_text("")

    with patch(
        "metergraphrelay.cli.push_file", return_value=(0, 0)
    ) as mock_push:
        main(["push", str(trace_file), "--env-file", str(env_file)])

    mock_push.assert_called_once_with(
        str(trace_file), "tok-123", base_url="http://localhost:8080"
    )


def test_pull_langfuse_help_documents_every_flag_and_default(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["pull", "langfuse", "--help"])

    # argparse's HelpFormatter word-wraps to terminal width, which can break
    # a single phrase in the source string (e.g. "OR'd together") across a
    # newline + indent. Collapse all whitespace runs to single spaces so
    # substring checks validate the semantic content, not incidental
    # terminal-width wrapping.
    help_text = " ".join(capsys.readouterr().out.split())

    for expected in [
        "--count",
        "default: 100",
        "--since",
        "no lower bound",
        "--until",
        "captured once",
        "--trace-name",
        "OR'd together",
        "--tag",
        "ALL given tags",
        "--environment",
        "--route",
        "Not a selector",
        "--base-url",
        "LANGFUSE_BASE_URL",
        "--output",
        "./traces.jsonl",
        "--env-file",
        ".env",
        "--langfuse-public-key",
        "LANGFUSE_PUBLIC_KEY",
        "--langfuse-secret-key",
        "LANGFUSE_SECRET_KEY",
    ]:
        assert expected in help_text, f"missing {expected!r} in --help output"


def test_pull_help_lists_langfuse_subcommand(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["pull", "--help"])

    assert "langfuse" in capsys.readouterr().out


def test_readme_pull_langfuse_examples_parse_successfully():
    readme_text = (Path(__file__).parent.parent / "README.md").read_text()

    assert "metergraphrelay pull langfuse -n 25 --output traces.jsonl" in readme_text
    assert (
        "metergraphrelay pull langfuse --since 2026-08-01T00:00:00Z "
        "--until 2026-08-07T00:00:00Z"
    ) in readme_text
    assert (
        "metergraphrelay pull langfuse --trace-name support-bot-reply "
        "--trace-name billing-bot-reply --tag prod --tag tier-1"
    ) in readme_text

    build_parser().parse_args(
        ["pull", "langfuse", "-n", "25", "--output", "traces.jsonl"]
    )
    build_parser().parse_args(
        [
            "pull",
            "langfuse",
            "--since",
            "2026-08-01T00:00:00Z",
            "--until",
            "2026-08-07T00:00:00Z",
        ]
    )
    build_parser().parse_args(
        [
            "pull",
            "langfuse",
            "--trace-name",
            "support-bot-reply",
            "--trace-name",
            "billing-bot-reply",
            "--tag",
            "prod",
            "--tag",
            "tier-1",
        ]
    )


def test_sync_portkey_manual_mode_still_dispatches_to_local_converter(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("METERGRAPH_APP_TOKEN=tok-123\n")
    export_file = tmp_path / "export.jsonl"
    export_file.write_text("")

    with patch("metergraphrelay.cli._run_sync_portkey", return_value=0) as manual, patch(
        "metergraphrelay.cli._run_sync_portkey_api"
    ) as api:
        exit_code = main(["sync", "portkey", str(export_file), "--env-file", str(env_file)])

    assert exit_code == 0
    manual.assert_called_once()
    api.assert_not_called()


def test_sync_portkey_no_export_file_dispatches_to_api_mode(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("PORTKEY_API_KEY=pk-1\nMETERGRAPH_APP_TOKEN=tok-123\n")

    with patch("metergraphrelay.cli._run_sync_portkey_api", return_value=0) as api, patch(
        "metergraphrelay.cli._run_sync_portkey"
    ) as manual:
        exit_code = main(
            ["sync", "portkey", "--source-scope", "ws-acme", "--env-file", str(env_file)]
        )

    assert exit_code == 0
    api.assert_called_once()
    manual.assert_not_called()


def test_sync_portkey_api_missing_portkey_credential_returns_error(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("METERGRAPH_APP_TOKEN=tok-123\n")

    exit_code = main(
        ["sync", "portkey", "--source-scope", "ws-acme", "--env-file", str(env_file)]
    )

    assert exit_code == 1
    assert "PORTKEY_API_KEY" in capsys.readouterr().err


def test_sync_portkey_api_missing_source_scope_returns_error(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("PORTKEY_API_KEY=pk-1\nMETERGRAPH_APP_TOKEN=tok-123\n")

    exit_code = main(["sync", "portkey", "--env-file", str(env_file)])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "source" in err.lower() or "PORTKEY_WORKSPACE" in err


def test_sync_portkey_api_source_scope_from_env(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "PORTKEY_API_KEY=pk-1\nMETERGRAPH_APP_TOKEN=tok-123\nPORTKEY_WORKSPACE=ws-from-env\n"
    )

    with patch(
        "metergraphrelay.cli.run_portkey_sync",
        return_value=SyncOutcome("completed", "done", 0),
    ) as run:
        exit_code = main(["sync", "portkey", "--env-file", str(env_file)])

    assert exit_code == 0
    assert run.call_args.kwargs["source_scope"] == "ws-from-env"


def test_sync_portkey_api_flag_overrides_env_source_scope(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "PORTKEY_API_KEY=pk-1\nMETERGRAPH_APP_TOKEN=tok-123\nPORTKEY_WORKSPACE=ws-from-env\n"
    )

    with patch(
        "metergraphrelay.cli.run_portkey_sync",
        return_value=SyncOutcome("completed", "done", 0),
    ) as run:
        main(["sync", "portkey", "--source-scope", "ws-flag", "--env-file", str(env_file)])

    assert run.call_args.kwargs["source_scope"] == "ws-flag"


def test_sync_portkey_api_passes_initial_since_and_max_window(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("PORTKEY_API_KEY=pk-1\nMETERGRAPH_APP_TOKEN=tok-123\n")

    with patch(
        "metergraphrelay.cli.run_portkey_sync",
        return_value=SyncOutcome("caught_up", "caught up", 0),
    ) as run:
        main(
            [
                "sync", "portkey", "--source-scope", "ws-acme",
                "--initial-since", "2026-08-01T00:00:00+00:00",
                "--env-file", str(env_file),
            ]
        )

    kwargs = run.call_args.kwargs
    assert kwargs["initial_since"] == "2026-08-01T00:00:00+00:00"
    assert kwargs["max_window_seconds"] == 3600


def test_sync_portkey_api_rejects_naive_initial_since(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("PORTKEY_API_KEY=pk-1\nMETERGRAPH_APP_TOKEN=tok-123\n")

    exit_code = main(
        [
            "sync", "portkey", "--source-scope", "ws-acme",
            "--initial-since", "2026-08-01T00:00:00", "--env-file", str(env_file),
        ]
    )

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "--initial-since" in err
    assert "aware" in err.lower() or "timezone" in err.lower()


def test_sync_portkey_api_rejects_max_window_over_3600(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("PORTKEY_API_KEY=pk-1\nMETERGRAPH_APP_TOKEN=tok-123\n")

    exit_code = main(
        [
            "sync", "portkey", "--source-scope", "ws-acme",
            "--max-window-seconds", "7200", "--env-file", str(env_file),
        ]
    )

    assert exit_code == 1
    assert "3600" in capsys.readouterr().err


def test_sync_portkey_output_flag_rejected_in_api_mode(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("PORTKEY_API_KEY=pk-1\nMETERGRAPH_APP_TOKEN=tok-123\n")

    exit_code = main(
        [
            "sync", "portkey", "--source-scope", "ws-acme",
            "--output", "converted.jsonl", "--env-file", str(env_file),
        ]
    )

    assert exit_code == 1
    assert "--output" in capsys.readouterr().err


def test_sync_portkey_api_base_url_override_passed_to_clients(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "PORTKEY_API_KEY=pk-1\nMETERGRAPH_APP_TOKEN=tok-123\n"
        "PORTKEY_BASE_URL=https://portkey.internal/v1\n"
        "METERGRAPH_INGEST_URL=https://mg.internal\n"
    )

    with patch(
        "metergraphrelay.cli.MeterGraphSyncClient"
    ) as mg_cls, patch(
        "metergraphrelay.cli.PortkeyExportClient"
    ) as pk_cls, patch(
        "metergraphrelay.cli.run_portkey_sync",
        return_value=SyncOutcome("caught_up", "caught up", 0),
    ) as run:
        exit_code = main(["sync", "portkey", "--source-scope", "ws-acme", "--env-file", str(env_file)])

    assert exit_code == 0
    assert mg_cls.call_args.args[0] == "https://mg.internal"
    assert pk_cls.call_args.kwargs["base_url"] == "https://portkey.internal/v1"
    assert run.call_args.kwargs["ingest_base_url"] == "https://mg.internal"


def test_sync_portkey_api_error_returns_clean_exit(tmp_path, capsys):
    from metergraphrelay.metergraph_sync import MeterGraphSyncError

    env_file = tmp_path / ".env"
    env_file.write_text("PORTKEY_API_KEY=pk-1\nMETERGRAPH_APP_TOKEN=tok-123\n")

    with patch(
        "metergraphrelay.cli.run_portkey_sync",
        side_effect=MeterGraphSyncError("acquire failed: HTTP 500 err"),
    ):
        exit_code = main(["sync", "portkey", "--source-scope", "ws-acme", "--env-file", str(env_file)])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert err.startswith("Error: ")
    assert "Traceback" not in err


def test_sync_portkey_api_completed_prints_detail_and_exits_zero(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("PORTKEY_API_KEY=pk-1\nMETERGRAPH_APP_TOKEN=tok-123\n")

    with patch(
        "metergraphrelay.cli.run_portkey_sync",
        return_value=SyncOutcome("completed", "Synced window; pushed 3 row(s), 0 failed.", 0),
    ):
        exit_code = main(["sync", "portkey", "--source-scope", "ws-acme", "--env-file", str(env_file)])

    assert exit_code == 0
    assert "pushed 3 row(s)" in capsys.readouterr().out


def test_sync_portkey_api_failed_outcome_returns_nonzero(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("PORTKEY_API_KEY=pk-1\nMETERGRAPH_APP_TOKEN=tok-123\n")

    with patch(
        "metergraphrelay.cli.run_portkey_sync",
        return_value=SyncOutcome("failed", "Portkey export failed.", 1),
    ):
        exit_code = main(["sync", "portkey", "--source-scope", "ws-acme", "--env-file", str(env_file)])

    assert exit_code == 1
    assert "Portkey export failed." in capsys.readouterr().out


def test_sync_portkey_api_busy_prints_and_exits_zero(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("PORTKEY_API_KEY=pk-1\nMETERGRAPH_APP_TOKEN=tok-123\n")

    with patch(
        "metergraphrelay.cli.run_portkey_sync",
        return_value=SyncOutcome("busy", "Another sync holds the lease; retry at X.", 0),
    ):
        exit_code = main(["sync", "portkey", "--source-scope", "ws-acme", "--env-file", str(env_file)])

    assert exit_code == 0
    assert "retry at" in capsys.readouterr().out.lower()


def test_sync_portkey_help_documents_api_mode(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["sync", "portkey", "--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    for expected in [
        "--source-scope", "--initial-since", "--max-window-seconds",
        "PORTKEY_API_KEY", "workspace",
    ]:
        assert expected in help_text, f"missing {expected!r} in --help output"


def test_readme_portkey_cron_example_parses():
    readme = (Path(__file__).parent.parent / "README.md").read_text()
    assert (
        "metergraphrelay sync portkey --source-scope ws-acme "
        "--initial-since 2026-08-01T00:00:00+00:00"
    ) in readme
    build_parser().parse_args(
        ["sync", "portkey", "--source-scope", "ws-acme",
         "--initial-since", "2026-08-01T00:00:00+00:00"]
    )


def test_docs_portkey_base_url_matches_client_default():
    # The documented default/public Portkey base must include the /v1 prefix
    # the client actually uses, so operators don't configure a base missing it.
    from metergraphrelay.providers.portkey_export import DEFAULT_PORTKEY_URL

    assert DEFAULT_PORTKEY_URL == "https://api.portkey.ai/v1"
    root = Path(__file__).parent.parent
    readme = (root / "README.md").read_text()
    env_example = (root / ".env.example").read_text()
    assert DEFAULT_PORTKEY_URL in readme
    assert DEFAULT_PORTKEY_URL in env_example
    # No bare host (missing /v1) left as a concrete example anywhere in the docs.
    assert "api.portkey.ai\n" not in readme
    assert "api.portkey.ai " not in readme
    assert "api.portkey.ai\n" not in env_example
    assert "api.portkey.ai " not in env_example
