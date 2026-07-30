import pytest


@pytest.fixture(autouse=True)
def clear_openai_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
