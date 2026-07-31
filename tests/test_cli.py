from unittest.mock import patch

from metergraphrelay.cli import main


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

    with patch("metergraphrelay.cli.OpenAI") as mock_openai_cls, patch(
        "metergraphrelay.cli.run_demo"
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

    with patch("metergraphrelay.cli.OpenAI") as mock_openai_cls, patch(
        "metergraphrelay.cli.export_traces", return_value=3
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

    with patch("metergraphrelay.cli.OpenAI"), patch(
        "metergraphrelay.cli.export_traces", return_value=0
    ):
        exit_code = main(
            ["export", "--env-file", str(env_file), "--output", str(output_path)]
        )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "openai-exporter demo" in captured.out
