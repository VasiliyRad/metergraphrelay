"""Which providers report input tokens without the cached ones.

MeterGraph prices cache handling per publisher, so a row has to arrive in the
shape that publisher's API uses. Anthropic (direct or on Bedrock) reports
input excluding the cache buckets it lists separately; OpenAI and Google
report totals that include them.
"""

from __future__ import annotations

CACHE_EXCLUSIVE_INPUT_PROVIDERS = frozenset(
    {"anthropic", "bedrock", "aws-bedrock", "aws", "amazon-bedrock"}
)


def reports_input_without_cache(provider: object, model: object = None) -> bool:
    """Whether this call's publisher counts input tokens without the cache
    buckets. A Claude model served elsewhere (Vertex, a gateway) keeps
    Anthropic's accounting."""
    name = str(provider or "").strip().lower()
    return name in CACHE_EXCLUSIVE_INPUT_PROVIDERS or str(model or "").strip().lower().startswith("claude")
