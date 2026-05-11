from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable, Iterator, TypeVar


T = TypeVar("T")
R = TypeVar("R")


class RoleScheduler:
    """Runs one role at a time while allowing same-role parallel workers."""

    def map_same_role(self, items: Iterable[T], worker: Callable[[T], R], max_workers: int) -> list[R]:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            return list(executor.map(worker, items))


class BackendBulkheadScheduler:
    def __init__(
        self,
        *,
        backend_limits: dict[str, int] | None = None,
        role_limits: dict[str, int] | None = None,
        global_limit: int | None = None,
        poll_seconds: float = 0.05,
    ):
        self.backend_limits = {key: value for key, value in (backend_limits or {}).items() if value > 0}
        self.role_limits = {key: value for key, value in (role_limits or {}).items() if value > 0}
        self.global_limit = global_limit if global_limit and global_limit > 0 else None
        self.poll_seconds = poll_seconds
        self._lock = threading.RLock()
        self._backend_semaphores: dict[str, threading.Semaphore] = {}
        self._role_semaphores: dict[str, threading.Semaphore] = {}
        self._global_semaphore = threading.Semaphore(self.global_limit) if self.global_limit else None

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "BackendBulkheadScheduler":
        scheduler_config = config.get("scheduler", {})
        if not isinstance(scheduler_config, dict):
            scheduler_config = {}
        backend_limits = config.get("backend_concurrency", scheduler_config.get("backend_concurrency", {}))
        role_limits = config.get("role_concurrency", scheduler_config.get("role_concurrency", {}))
        return cls(
            backend_limits=cls._positive_int_map(backend_limits),
            role_limits=cls._positive_int_map(role_limits),
            global_limit=cls._positive_int(scheduler_config.get("global_concurrency") or config.get("global_concurrency")),
        )

    @contextmanager
    def acquire(
        self,
        *,
        backend: str,
        role: str,
        cancel_event: threading.Event | None = None,
    ) -> Iterator[None]:
        semaphores = self._semaphores_for(backend, role)
        acquired: list[threading.Semaphore] = []
        try:
            for semaphore in semaphores:
                self._acquire_with_cancel(semaphore, cancel_event)
                acquired.append(semaphore)
            yield
        finally:
            for semaphore in reversed(acquired):
                semaphore.release()

    def _semaphores_for(self, backend: str, role: str) -> list[threading.Semaphore]:
        semaphores: list[threading.Semaphore] = []
        if self._global_semaphore:
            semaphores.append(self._global_semaphore)
        backend_limit = self.backend_limits.get(backend) or self.backend_limits.get("default") or self.backend_limits.get("*")
        if backend_limit:
            semaphores.append(self._backend_semaphore(backend, backend_limit))
        role_limit = self.role_limits.get(role) or self.role_limits.get("default") or self.role_limits.get("*")
        if role_limit:
            semaphores.append(self._role_semaphore(role, role_limit))
        return semaphores

    def _backend_semaphore(self, backend: str, limit: int) -> threading.Semaphore:
        with self._lock:
            return self._backend_semaphores.setdefault(backend, threading.Semaphore(limit))

    def _role_semaphore(self, role: str, limit: int) -> threading.Semaphore:
        with self._lock:
            return self._role_semaphores.setdefault(role, threading.Semaphore(limit))

    def _acquire_with_cancel(self, semaphore: threading.Semaphore, cancel_event: threading.Event | None) -> None:
        while True:
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("Scheduler bulkhead wait cancelled")
            if semaphore.acquire(timeout=self.poll_seconds):
                return
            time.sleep(0)

    @staticmethod
    def _positive_int_map(value: Any) -> dict[str, int]:
        if not isinstance(value, dict):
            return {}
        result: dict[str, int] = {}
        for key, raw in value.items():
            parsed = BackendBulkheadScheduler._positive_int(raw)
            if parsed:
                result[str(key)] = parsed
        return result

    @staticmethod
    def _positive_int(value: Any) -> int | None:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None
