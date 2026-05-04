from __future__ import annotations

from concurrent.futures import Future, wait
from typing import Iterable


def wait_for_all(futures: Iterable[Future], timeout: float | None = None):
    return wait(list(futures), timeout=timeout)

