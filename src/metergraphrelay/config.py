from __future__ import annotations

import os

from dotenv import load_dotenv


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


CREDENTIAL_SPECS: dict[str, list[str]] = {
    "openai": ["OPENAI_API_KEY"],
    "anthropic": ["ANTHROPIC_API_KEY"],
    "langfuse": ["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"],
    "portkey": ["PORTKEY_API_KEY"],
    "push": ["METERGRAPH_APP_TOKEN"],
}


def require_credentials(target: str, env_file: str = ".env") -> dict[str, str]:
    load_dotenv(env_file, override=True)
    var_names = CREDENTIAL_SPECS[target]
    values: dict[str, str] = {}
    missing: list[str] = []
    for name in var_names:
        value = os.environ.get(name, "").strip()
        if value:
            values[name] = value
        else:
            missing.append(name)
    if missing:
        joined = ", ".join(missing)
        example = "\n".join(f"{name}=<value>" for name in missing)
        raise ConfigError(
            f"{joined} not set. Add to {env_file}:\n{example}\n"
            f"(or export {'it' if len(missing) == 1 else 'them'} in your shell)"
        )
    return values
