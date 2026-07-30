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
