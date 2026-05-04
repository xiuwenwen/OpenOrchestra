from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable, TypeVar


T = TypeVar("T")
R = TypeVar("R")


class RoleScheduler:
    """Runs one role at a time while allowing same-role parallel workers."""

    def map_same_role(self, items: Iterable[T], worker: Callable[[T], R], max_workers: int) -> list[R]:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            return list(executor.map(worker, items))

