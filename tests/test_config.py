import pytest

from metergraphrelay.config import CREDENTIAL_SPECS, ConfigError, require_credentials


def test_credential_specs_cover_all_targets():
    assert CREDENTIAL_SPECS == {
        "openai": ["OPENAI_API_KEY"],
        "anthropic": ["ANTHROPIC_API_KEY"],
        "langfuse": ["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"],
        "braintrust": ["BRAINTRUST_API_KEY"],
        "langsmith": ["LANGSMITH_API_KEY"],
        "portkey": ["PORTKEY_API_KEY"],
        "push": ["METERGRAPH_APP_TOKEN"],
    }


def test_require_credentials_reads_single_var_from_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=sk-test-123\n")

    result = require_credentials("openai", str(env_file))

    assert result == {"OPENAI_API_KEY": "sk-test-123"}


def test_require_credentials_reads_multiple_vars_from_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("LANGFUSE_PUBLIC_KEY=pk-1\nLANGFUSE_SECRET_KEY=sk-1\n")

    result = require_credentials("langfuse", str(env_file))

    assert result == {"LANGFUSE_PUBLIC_KEY": "pk-1", "LANGFUSE_SECRET_KEY": "sk-1"}


def test_require_credentials_raises_when_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("")

    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        require_credentials("anthropic", str(env_file))


def test_require_credentials_raises_when_blank(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=   \n")

    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        require_credentials("openai", str(env_file))


def test_require_credentials_lists_all_missing_vars_for_multi_var_target(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("LANGFUSE_PUBLIC_KEY=pk-1\n")

    with pytest.raises(ConfigError) as excinfo:
        require_credentials("langfuse", str(env_file))

    assert "LANGFUSE_SECRET_KEY" in str(excinfo.value)


def test_require_credentials_env_file_overrides_existing_shell_value(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("METERGRAPH_APP_TOKEN", "stale-shell-value")
    env_file = tmp_path / ".env"
    env_file.write_text("METERGRAPH_APP_TOKEN=fresh-file-value\n")

    result = require_credentials("push", str(env_file))

    assert result == {"METERGRAPH_APP_TOKEN": "fresh-file-value"}
