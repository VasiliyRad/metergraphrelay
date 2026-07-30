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
