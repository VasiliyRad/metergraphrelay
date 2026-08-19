import pytest

from metergraphrelay.config import CREDENTIAL_SPECS

# Every env var the CLI reads. Credentials come from CREDENTIAL_SPECS so this
# list cannot drift; METERGRAPH_INGEST_URL is added explicitly because it is
# optional (no ConfigError check) and therefore not in CREDENTIAL_SPECS.
ENV_VARS_READ_BY_CLI = sorted(
    {name for names in CREDENTIAL_SPECS.values() for name in names}
    | {"METERGRAPH_INGEST_URL", "LANGFUSE_BASE_URL", "PORTKEY_WORKSPACE", "PORTKEY_BASE_URL"}
)


@pytest.fixture(autouse=True)
def clear_cli_env_vars(monkeypatch):
    """Isolate tests from the developer's / CI's real environment.

    ``require_credentials`` calls ``load_dotenv(..., override=True)``, which
    mutates ``os.environ`` process-wide and is not undone by monkeypatch, so
    this also keeps tests from leaking values into each other.
    """
    for name in ENV_VARS_READ_BY_CLI:
        monkeypatch.delenv(name, raising=False)
