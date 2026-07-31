# Examples

Runnable samples that produce data `metergraphrelay` can pull. These
are not part of the installed package.

Setup:

    cp .env.example .env
    # edit .env and set OPENAI_API_KEY

| Example | Needs |
|---|---|
| `openai/main.py` | `OPENAI_API_KEY` in `.env` |

Run an example, then pull what it created:

    python examples/openai/main.py
    metergraphrelay pull openai
