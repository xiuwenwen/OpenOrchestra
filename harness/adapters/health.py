from __future__ import annotations

import time
import re
from dataclasses import dataclass
from typing import Any, Callable


AUTH_FAILURE_PATTERNS = (
    "authentication",
    "unauthorized",
    "invalid api key",
    "permission denied",
)
REQUEST_SIZE_PATTERNS = (
    "request exceeded",
    "request-size",
    "context/request-size",
    "context window",
    "maximum context length",
    "too many tokens",
)
OUTPUT_CONTRACT_PATTERNS = (
    "missing required output",
    "output contract",
    "artifact_result_code",
    "delivery.md",
)


@dataclass(frozen=True)
class BackendHealthSnapshot:
    backend: str
    state: str
    allowed: bool
    consecutive_failures: int
    failure_kind: str | None = None
    open_until: float | None = None
    reason: str | None = None


@dataclass
class _BackendHealthState:
    state: str = "healthy"
    consecutive_failures: int = 0
    failure_kind: str | None = None
    open_until: float | None = None
    reason: str | None = None


class BackendHealthMonitor:
    def __init__(
        self,
        *,
        enabled: bool = True,
        failure_threshold: int = 3,
        cooldown_seconds: float = 120.0,
        time_provider: Callable[[], float] = time.monotonic,
    ):
        self.enabled = enabled
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self.time_provider = time_provider
        self._states: dict[str, _BackendHealthState] = {}

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "BackendHealthMonitor":
        settings = config.get("backend_health", {})
        if not isinstance(settings, dict):
            settings = {}
        return cls(
            enabled=bool(settings.get("enabled", True)),
            failure_threshold=int(settings.get("failure_threshold", 3)),
            cooldown_seconds=float(settings.get("cooldown_seconds", 120)),
        )

    def check(self, backend: str) -> BackendHealthSnapshot:
        state = self._state_for(backend)
        now = self.time_provider()
        if not self.enabled:
            return self._snapshot(backend, state, allowed=True)
        if state.state == "open" and state.open_until is not None and now >= state.open_until:
            state.state = "degraded"
            state.open_until = None
            state.reason = "cooldown expired; allowing a probe attempt"
        return self._snapshot(backend, state, allowed=state.state != "open")

    def record_success(self, backend: str) -> BackendHealthSnapshot:
        state = self._state_for(backend)
        state.state = "healthy"
        state.consecutive_failures = 0
        state.failure_kind = None
        state.open_until = None
        state.reason = None
        return self._snapshot(backend, state, allowed=True)

    def record_failure(self, backend: str, message: str, *, status: str | None = None) -> BackendHealthSnapshot:
        state = self._state_for(backend)
        kind = self.classify_failure(message, status=status)
        if not self.enabled or kind in {"request_size", "output_contract"}:
            return self._snapshot(backend, state, allowed=True, failure_kind=kind)

        state.consecutive_failures += 1
        state.failure_kind = kind
        if kind == "auth" or state.consecutive_failures >= self.failure_threshold:
            state.state = "open"
            state.open_until = self.time_provider() + self.cooldown_seconds
            state.reason = f"backend {backend} circuit opened after {state.consecutive_failures} {kind} failure(s)"
        else:
            state.state = "degraded"
            state.reason = f"backend {backend} degraded after {state.consecutive_failures} {kind} failure(s)"
        return self._snapshot(backend, state, allowed=state.state != "open")

    def classify_failure(self, message: str, *, status: str | None = None) -> str:
        text = f"{status or ''}\n{message}".lower()
        if any(pattern in text for pattern in AUTH_FAILURE_PATTERNS) or re.search(
            r"(http|status|code)\s*(401|403)|\b(401|403)\s+(unauthorized|forbidden)",
            text,
        ):
            return "auth"
        if "timeout" in text or "timed out" in text or "exit_code=124" in text:
            return "timeout"
        if any(pattern in text for pattern in REQUEST_SIZE_PATTERNS):
            return "request_size"
        if "agent exit_code=" in text and "agent exit_code=0" not in text:
            return "runtime_error"
        if any(pattern in text for pattern in OUTPUT_CONTRACT_PATTERNS) or str(status or "").upper() == "OUTPUT_INVALID":
            return "output_contract"
        return "runtime_error"

    def _state_for(self, backend: str) -> _BackendHealthState:
        return self._states.setdefault(backend, _BackendHealthState())

    def _snapshot(
        self,
        backend: str,
        state: _BackendHealthState,
        *,
        allowed: bool,
        failure_kind: str | None = None,
    ) -> BackendHealthSnapshot:
        return BackendHealthSnapshot(
            backend=backend,
            state=state.state,
            allowed=allowed,
            consecutive_failures=state.consecutive_failures,
            failure_kind=failure_kind or state.failure_kind,
            open_until=state.open_until,
            reason=state.reason,
        )
