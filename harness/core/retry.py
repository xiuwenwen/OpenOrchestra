from __future__ import annotations


def should_retry(attempt: int, max_retry: int) -> bool:
    return attempt < max_retry

