from __future__ import annotations

import time
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping


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
AGENT_RUNTIME_PATTERNS = (
    "api error: unable to connect to api",
    "connectionrefused",
    "failedtoopensocket",
    "temporary failure in name resolution",
    "unable to find image",
    "pull access denied",
    "repository does not exist",
    "docker daemon",
    "cannot connect to the docker daemon",
    "docker binary not found",
    "docker: command not found",
    "error response from daemon",
    "no such image",
    "settings file not found",
)
NON_BACKEND_FAILURE_KINDS = {"request_size", "output_contract", "agent_runtime"}


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
        time_provider: Callable[[], float] = time.time,
        persisted_states: Mapping[str, Mapping[str, Any]] | None = None,
        persist_callback: Callable[[BackendHealthSnapshot], None] | None = None,
    ):
        self.enabled = enabled
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self.time_provider = time_provider
        self._persist_callback = persist_callback
        self._states: dict[str, _BackendHealthState] = {}
        for backend, persisted in (persisted_states or {}).items():
            self._states[backend] = _BackendHealthState(
                state=str(persisted.get("state") or "healthy"),
                consecutive_failures=int(persisted.get("consecutive_failures") or 0),
                failure_kind=persisted.get("failure_kind"),
                open_until=_optional_float(persisted.get("open_until")),
                reason=persisted.get("reason"),
            )

    @classmethod
    def from_config(
        cls,
        config: dict[str, Any],
        *,
        persisted_states: Mapping[str, Mapping[str, Any]] | None = None,
        persist_callback: Callable[[BackendHealthSnapshot], None] | None = None,
    ) -> "BackendHealthMonitor":
        settings = config.get("backend_health", {})
        if not isinstance(settings, dict):
            settings = {}
        return cls(
            enabled=bool(settings.get("enabled", True)),
            failure_threshold=int(settings.get("failure_threshold", 3)),
            cooldown_seconds=float(settings.get("cooldown_seconds", 120)),
            persisted_states=persisted_states,
            persist_callback=persist_callback,
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
            self._persist(backend, state, allowed=True)
        return self._snapshot(backend, state, allowed=state.state != "open")

    def record_success(self, backend: str) -> BackendHealthSnapshot:
        state = self._state_for(backend)
        state.state = "healthy"
        state.consecutive_failures = 0
        state.failure_kind = None
        state.open_until = None
        state.reason = None
        self._persist(backend, state, allowed=True)
        return self._snapshot(backend, state, allowed=True)

    def record_failure(self, backend: str, message: str, *, status: str | None = None) -> BackendHealthSnapshot:
        state = self._state_for(backend)
        kind = self.classify_failure(message, status=status)
        if not self.enabled or kind in NON_BACKEND_FAILURE_KINDS:
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
        self._persist(backend, state, allowed=state.state != "open", failure_kind=kind)
        return self._snapshot(backend, state, allowed=state.state != "open")

    def cooldown_backend(self, backend: str, cooldown_seconds: float, *, reason: str | None = None) -> BackendHealthSnapshot:
        state = self._state_for(backend)
        state.state = "open"
        state.open_until = self.time_provider() + max(0.0, float(cooldown_seconds))
        state.reason = reason or f"backend {backend} manually cooled down for {int(max(0.0, float(cooldown_seconds)))}s"
        if state.consecutive_failures < 1:
            state.consecutive_failures = 1
        state.failure_kind = state.failure_kind or "manual_cooldown"
        self._persist(backend, state, allowed=False)
        return self._snapshot(backend, state, allowed=False)

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
        if any(pattern in text for pattern in AGENT_RUNTIME_PATTERNS):
            return "agent_runtime"
        if any(pattern in text for pattern in OUTPUT_CONTRACT_PATTERNS) or str(status or "").upper() == "OUTPUT_INVALID":
            return "output_contract"
        if "agent exit_code=" in text and "agent exit_code=0" not in text:
            return "runtime_error"
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

    def _persist(
        self,
        backend: str,
        state: _BackendHealthState,
        *,
        allowed: bool,
        failure_kind: str | None = None,
    ) -> None:
        if self._persist_callback is None:
            return
        self._persist_callback(self._snapshot(backend, state, allowed=allowed, failure_kind=failure_kind))


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
