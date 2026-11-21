"""Fault injection for chaos testing. Configured per dependency name (glob-ish prefix match).

Example: FaultInjector({"tool:shipping": Fault(error_rate=0.3), "llm:*": Fault(timeout_rate=0.2)})
Modes:
  error_rate     -> raise TransientError (like a 503)
  timeout_rate   -> sleep past the call timeout -> DependencyTimeout
  malformed_rate -> the call "succeeds" but returns garbage (only meaningful for LLM text)
  down           -> every call fails (simulates an outage; breaker should open)
  latency_ms     -> extra latency on every call
"""
from __future__ import annotations

import random
import threading
from dataclasses import dataclass, field

from .errors import TransientError


@dataclass
class Fault:
    error_rate: float = 0.0
    timeout_rate: float = 0.0
    malformed_rate: float = 0.0
    down: bool = False
    latency_ms: float = 0.0


@dataclass
class FaultDecision:
    raise_error: bool = False
    timeout: bool = False
    malformed: bool = False
    latency_ms: float = 0.0


class FaultInjector:
    def __init__(self, faults: dict[str, Fault] | None = None, seed: int | None = None):
        self.faults: dict[str, Fault] = dict(faults or {})
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self.injected: dict[str, int] = {}

    def _match(self, dep: str) -> Fault | None:
        if dep in self.faults:
            return self.faults[dep]
        for pattern, f in self.faults.items():
            if pattern.endswith("*") and dep.startswith(pattern[:-1]):
                return f
        return None

    def decide(self, dep: str) -> FaultDecision:
        f = self._match(dep)
        if not f:
            return FaultDecision()
        with self._lock:
            r = self._rng.random()
        d = FaultDecision(latency_ms=f.latency_ms)
        if f.down:
            d.raise_error = True
        elif r < f.error_rate:
            d.raise_error = True
        elif r < f.error_rate + f.timeout_rate:
            d.timeout = True
        elif r < f.error_rate + f.timeout_rate + f.malformed_rate:
            d.malformed = True
        if d.raise_error or d.timeout or d.malformed:
            with self._lock:
                self.injected[dep] = self.injected.get(dep, 0) + 1
        return d

    @staticmethod
    def injected_error(dep: str) -> TransientError:
        return TransientError(f"[chaos] injected failure in {dep}", dependency=dep)
