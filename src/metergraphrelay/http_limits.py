"""Small bounds shared by provider HTTP clients."""

from __future__ import annotations


MAX_JSON_RESPONSE_BYTES = 2 * 1024 * 1024
READ_CHUNK_SIZE = 64 * 1024


class ResponseTooLarge(ValueError):
    """Raised when a provider response exceeds the JSON body budget."""


def read_bounded(response, *, max_bytes: int = MAX_JSON_RESPONSE_BYTES) -> bytes:
    """Read a response without allowing an untrusted body to fill memory."""
    declared = response.headers.get("Content-Length")
    if declared:
        try:
            declared_bytes = int(declared)
        except (TypeError, ValueError):
            pass
        else:
            if declared_bytes > max_bytes:
                raise ResponseTooLarge(
                    f"response exceeds the {max_bytes}-byte JSON body limit"
                )

    # ``read(n)`` is bounded by the requested byte count for urllib response
    # objects. Reading one byte beyond the budget detects oversized bodies
    # without ever retaining more than the allowed response plus one byte.
    body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ResponseTooLarge(
            f"response exceeds the {max_bytes}-byte JSON body limit"
        )
    return body
