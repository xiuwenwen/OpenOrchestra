from __future__ import annotations

from abc import ABC, abstractmethod

from harness.agents.context import AgentRunContext
from harness.agents.result import AgentRunResult


class AgentAdapter(ABC):
    @abstractmethod
    def run(self, context: AgentRunContext) -> AgentRunResult:
        raise NotImplementedError

