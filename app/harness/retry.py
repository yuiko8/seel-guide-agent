"""Retry policy: classify -> decide -> back off with full jitter, never past the deadline.

Rules
- Only `retryable` errors are retried (transient / timeout / rate-limited).
- Non-idempotent calls are retried only if the caller supplies an idempotency key.
- Backoff = random(0, min(cap, base * 2**attempt)); honours Retry-After when given.
- A retry that would not fit in the remaining turn budget is not attempted.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass

from .deadline import Deadline
from .errors import DeadlineExceeded, HarnessError, RateLimited


@dataclass
class RetryPolicy:
    max_attempts: int = 3
    base_delay_s: float = 0.2
    max_delay_s: float = 2.0

    def delay(self, attempt: int, err: BaseException, rng: random.Random) -> float:
        if isinstance(err, RateLimited) and err.retry_after:
            return min(err.retry_after, self.max_delay_s)
        return rng.uniform(0, min(self.max_delay_s, self.base_delay_s * (2 ** attempt)))


NO_RETRY = RetryPolicy(max_attempts=1)


def should_retry(err: BaseException, *, idempotent: bool) -> bool:
    if not isinstance(err, HarnessError):
        return False
    return err.retryable and idempotent


def run_with_retry(fn, *, policy: RetryPolicy, deadline: Deadline, idempotent: bool,
                   on_attempt=None, sleep=time.sleep, rng: random.Random | None = None):
    """fn(attempt:int, timeout_s:float) -> result. `on_attempt(attempt, err|None, delay)` for tracing."""
    rng = rng or random.Random()
    last: BaseException | None = None
    for attempt in range(policy.max_attempts):
        try:
            result = fn(attempt)
            if on_attempt:
                on_attempt(attempt, None, 0.0)
            return result
        except BaseException as err:  # noqa: BLE001 - we classify below
            last = err
            final = attempt == policy.max_attempts - 1 or not should_retry(err, idempotent=idempotent)
            wait = 0.0 if final else policy.delay(attempt, err, rng)
            if not final and wait >= deadline.remaining():
                final = True
            if on_attempt:
                on_attempt(attempt, err, wait)
            if final:
                raise
            sleep(wait)
    raise last if last else DeadlineExceeded("retry loop exhausted")
