"""Sliding-window circuit breaker.

closed     -> calls flow; outcomes recorded in a window of the last `window` calls.
open       -> tripped when window has >= min_calls and error rate >= threshold,
              or after `consecutive_failures` in a row. Calls fail fast with CircuitOpen.
half_open  -> after `open_seconds`, up to `half_open_max` probe calls are let through;
              `half_open_successes` successes close it, any failure re-opens it.

Only failures that indicate the *dependency* is unhealthy count (transient/timeout).
A 400 caused by our own bad input does not trip the breaker.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field

from .errors import CircuitOpen, HarnessError


@dataclass
class BreakerConfig:
    window: int = 20
    min_calls: int = 5
    error_rate_threshold: float = 0.5
    consecutive_failures: int = 5
    open_seconds: float = 15.0
    half_open_max: int = 2
    half_open_successes: int = 2


@dataclass
class BreakerEvent:
    ts: float
    name: str
    from_state: str
    to_state: str
    reason: str


class CircuitBreaker:
    def __init__(self, name: str, config: BreakerConfig | None = None, clock=time.monotonic, on_transition=None):
        self.name = name
        self.cfg = config or BreakerConfig()
        self._clock = clock
        self._lock = threading.Lock()
        self._state = "closed"
        self._window: deque[bool] = deque(maxlen=self.cfg.window)
        self._consecutive = 0
        self._opened_at = 0.0
        self._half_open_inflight = 0
        self._half_open_ok = 0
        self._on_transition = on_transition
        self.stats = {"calls": 0, "failures": 0, "rejected": 0}

    # ---- state -------------------------------------------------------------
    @property
    def state(self) -> str:
        with self._lock:
            self._maybe_half_open()
            return self._state

    def _transition(self, to: str, reason: str) -> None:
        frm = self._state
        if frm == to:
            return
        self._state = to
        if to == "open":
            self._opened_at = self._clock()
        if to in ("closed", "half_open"):
            self._half_open_inflight = 0
            self._half_open_ok = 0
        if to == "closed":
            self._window.clear()
            self._consecutive = 0
        if self._on_transition:
            self._on_transition(BreakerEvent(time.time(), self.name, frm, to, reason))

    def _maybe_half_open(self) -> None:
        if self._state == "open" and self._clock() - self._opened_at >= self.cfg.open_seconds:
            self._transition("half_open", "open_seconds elapsed")

    # ---- call protocol -------------------------------------------------------
    def before_call(self) -> None:
        with self._lock:
            self._maybe_half_open()
            if self._state == "open":
                self.stats["rejected"] += 1
                raise CircuitOpen(f"circuit '{self.name}' is open", dependency=self.name)
            if self._state == "half_open":
                if self._half_open_inflight >= self.cfg.half_open_max:
                    self.stats["rejected"] += 1
                    raise CircuitOpen(f"circuit '{self.name}' half-open, probe slots full", dependency=self.name)
                self._half_open_inflight += 1
            self.stats["calls"] += 1

    def on_success(self) -> None:
        with self._lock:
            self._consecutive = 0
            if self._state == "half_open":
                self._half_open_inflight = max(0, self._half_open_inflight - 1)
                self._half_open_ok += 1
                if self._half_open_ok >= self.cfg.half_open_successes:
                    self._transition("closed", "half-open probes succeeded")
                return
            self._window.append(True)

    def on_failure(self, err: BaseException) -> None:
        counts = not isinstance(err, HarnessError) or err.retryable
        with self._lock:
            if not counts:
                if self._state == "half_open":
                    self._half_open_inflight = max(0, self._half_open_inflight - 1)
                return
            self.stats["failures"] += 1
            if self._state == "half_open":
                self._transition("open", f"probe failed: {type(err).__name__}")
                return
            self._window.append(False)
            self._consecutive += 1
            n = len(self._window)
            rate = self._window.count(False) / n if n else 0.0
            if self._consecutive >= self.cfg.consecutive_failures:
                self._transition("open", f"{self._consecutive} consecutive failures")
            elif n >= self.cfg.min_calls and rate >= self.cfg.error_rate_threshold:
                self._transition("open", f"error rate {rate:.0%} over last {n} calls")

    def snapshot(self) -> dict:
        with self._lock:
            self._maybe_half_open()
            n = len(self._window)
            return {
                "name": self.name,
                "state": self._state,
                "window_error_rate": round(self._window.count(False) / n, 3) if n else 0.0,
                "window_size": n,
                **self.stats,
            }

    def force(self, state: str) -> None:
        """Ops/testing hook: manually open or close a breaker."""
        with self._lock:
            self._transition(state, "forced")


class BreakerRegistry:
    def __init__(self, default: BreakerConfig | None = None, overrides: dict[str, BreakerConfig] | None = None, clock=time.monotonic):
        self._default = default or BreakerConfig()
        self._overrides = overrides or {}
        self._clock = clock
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()
        self.events: deque[BreakerEvent] = deque(maxlen=200)

    def get(self, name: str) -> CircuitBreaker:
        with self._lock:
            if name not in self._breakers:
                self._breakers[name] = CircuitBreaker(
                    name, self._overrides.get(name, self._default), clock=self._clock, on_transition=self.events.append
                )
            return self._breakers[name]

    def snapshot(self) -> list[dict]:
        return [b.snapshot() for b in list(self._breakers.values())]
