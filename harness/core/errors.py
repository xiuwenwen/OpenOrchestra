class HarnessError(Exception):
    """Base error for harness failures."""


class OutputInvalidError(HarnessError):
    """Raised when an agent does not produce required artifacts."""


class TaskFailedError(HarnessError):
    """Raised when a task cannot complete within policy limits."""

