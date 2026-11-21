"""Error taxonomy. Every failure inside the harness is mapped to one of these so
that retry / breaker / degrade decisions are made on *kind*, not on strings."""
from __future__ import annotations


class HarnessError(Exception):
    retryable: bool = False
    kind: str = "internal"

    def __init__(self, message: str = "", *, dependency: str | None = None):
        super().__init__(message)
        self.dependency = dependency


class TransientError(HarnessError):
    """Timeouts, 429, 5xx, connection resets. Safe to retry if the call is idempotent."""
    retryable = True
    kind = "transient"


class DependencyTimeout(TransientError):
    kind = "timeout"


class RateLimited(TransientError):
    kind = "rate_limited"

    def __init__(self, message: str = "", *, dependency: str | None = None, retry_after: float | None = None):
        super().__init__(message, dependency=dependency)
        self.retry_after = retry_after


class PermanentError(HarnessError):
    """400s, schema violations, auth. Retrying will not help."""
    retryable = False
    kind = "permanent"


class InvalidToolInput(PermanentError):
    kind = "invalid_input"


class MalformedOutput(PermanentError):
    """LLM returned something we cannot parse/validate. Gets one repair attempt, not a blind retry."""
    kind = "malformed_output"


class CircuitOpen(HarnessError):
    """Fast-fail: the dependency's breaker is open. Never retried; go straight to degrade."""
    retryable = False
    kind = "circuit_open"


class DeadlineExceeded(HarnessError):
    """The turn-level time budget is spent. Not retried."""
    retryable = False
    kind = "deadline"


class Refused(PermanentError):
    kind = "refusal"
