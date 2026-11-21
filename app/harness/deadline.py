"""Turn-level deadline. One Deadline is created per user turn and passed down to every
dependency call, so a slow step cannot silently eat the whole turn's budget."""
from __future__ import annotations

import time

from .errors import DeadlineExceeded


class Deadline:
    def __init__(self, budget_s: float, clock=time.monotonic):
        self._clock = clock
        self.budget_s = budget_s
        self.start = clock()
        self.end = self.start + budget_s

    def remaining(self) -> float:
        return max(0.0, self.end - self._clock())

    def expired(self) -> bool:
        return self.remaining() <= 0

    def elapsed(self) -> float:
        return self._clock() - self.start

    def check(self, what: str = "") -> None:
        if self.expired():
            raise DeadlineExceeded(f"turn deadline exceeded before {what or 'step'}")

    def cap(self, timeout_s: float) -> float:
        """Per-call timeout = min(call's own timeout, what is left of the turn)."""
        self.check()
        return min(timeout_s, self.remaining())
