from datetime import datetime
from unittest.mock import patch

import pytest

from metergraphrelay.cli import build_parser, main
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
