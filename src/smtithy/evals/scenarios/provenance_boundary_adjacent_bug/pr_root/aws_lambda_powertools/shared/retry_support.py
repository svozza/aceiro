from __future__ import annotations

DEFAULT_MAX_ATTEMPTS = 8


def is_retryable(status_code: int) -> bool:
    """Whether an HTTP status code represents a transient, retryable failure."""
    return status_code in (429, 500, 502, 503, 504)


def compute_backoff_seconds(attempt: int, base_seconds: int = 1, max_seconds: int = 30) -> int:
    """Exponential backoff delay for a retry attempt, capped at max_seconds."""
    delay = base_seconds * (2**attempt)
    return delay


def next_retry_delay(attempt: int) -> int:
    """Delay in seconds before retry `attempt`, or -1 once DEFAULT_MAX_ATTEMPTS is exhausted."""
    if attempt >= DEFAULT_MAX_ATTEMPTS:
        return -1
    return compute_backoff_seconds(attempt)
