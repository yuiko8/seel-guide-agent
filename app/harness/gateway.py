"""Dependency gateway: the single choke point for every tool / retrieval / LLM call.

For each call, in order:
  1. input validation against the tool's pydantic schema (InvalidToolInput, never retried)
  2. idempotency replay for side-effecting calls (same key -> same result, no second side effect)
  3. circuit breaker admission (CircuitOpen -> caller degrades immediately)
  4. per-call timeout, capped by the turn deadline
  5. chaos fault injection (only when a FaultInjector is configured)
  6. retry with backoff for transient errors on idempotent calls
  7. breaker bookkeeping + a trace span with attempts, latency and outcome
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from .circuit_breaker import BreakerRegistry
from .deadline import Deadline
from .errors import DependencyTimeout, HarnessError, InvalidToolInput, MalformedOutput, TransientError
from .faults import FaultInjector
from .retry import NO_RETRY, RetryPolicy, run_with_retry
from .tracing import Trace


@dataclass
class CallSpec:
    dependency: str                  # e.g. "tool:catalog", "llm:claude-opus-5-5", "retrieval:bm25"
    timeout_s: float = 3.0
    idempotent: bool = True
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    input_model: type[BaseModel] | None = None


class IdempotencyStore:
    """Interface; SessionStore implements it with SQLite."""

    def get(self, key: str) -> Any | None: ...
    def put(self, key: str, value: Any) -> None: ...


class Gateway:
    def __init__(self, breakers: BreakerRegistry, faults: FaultInjector | None = None,
                 idem: IdempotencyStore | None = None, max_workers: int = 16, sleep=time.sleep):
        self.breakers = breakers
        self.faults = faults or FaultInjector()
        self.idem = idem
        self._pool = cf.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="gw")
        self._sleep = sleep

    def call(self, spec: CallSpec, fn: Callable[..., Any], args: dict | None = None, *,
             trace: Trace, deadline: Deadline, idempotency_key: str | None = None,
             post_validate: Callable[[Any], Any] | None = None, span_attrs: dict | None = None) -> Any:
        args = dict(args or {})
        with trace.span(spec.dependency.split(":")[0] if ":" in spec.dependency else "tool", spec.dependency,
                        **(span_attrs or {})) as span:
            # 1. schema validation
            if spec.input_model is not None:
                try:
                    args = spec.input_model(**args).model_dump()
                except ValidationError as e:
                    span.attrs["validation"] = e.errors(include_url=False)[:3]
                    raise InvalidToolInput(f"{spec.dependency}: invalid input: {e.error_count()} errors", dependency=spec.dependency)
            span.attrs["args"] = _short(args)

            # 2. idempotent replay
            if idempotency_key and self.idem is not None:
                cached = self.idem.get(f"{spec.dependency}:{idempotency_key}")
                if cached is not None:
                    span.attrs["idempotent_replay"] = True
                    return cached

            breaker = self.breakers.get(spec.dependency)
            safe_to_retry = spec.idempotent or idempotency_key is not None
            attempts: list[dict] = []

            def attempt(n: int):
                breaker.before_call()          # 3. may raise CircuitOpen (not retried)
                timeout = deadline.cap(spec.timeout_s)  # 4. may raise DeadlineExceeded
                decision = self.faults.decide(spec.dependency)  # 5. chaos
                t0 = time.perf_counter()
                try:
                    if decision.latency_ms:
                        self._sleep(decision.latency_ms / 1000)
                    if decision.raise_error:
                        raise FaultInjector.injected_error(spec.dependency)
                    if decision.timeout:
                        self._sleep(min(timeout, 0.05))
                        raise DependencyTimeout(f"[chaos] {spec.dependency} timed out", dependency=spec.dependency)
                    fut = self._pool.submit(fn, **args, **({"timeout_s": timeout} if _accepts_timeout(fn) else {}))
                    try:
                        result = fut.result(timeout=timeout)
                    except cf.TimeoutError:
                        fut.cancel()
                        raise DependencyTimeout(f"{spec.dependency} exceeded {timeout:.2f}s", dependency=spec.dependency)
                    if decision.malformed:
                        result = _corrupt(result)
                    if post_validate is not None:
                        result = post_validate(result)
                except HarnessError as e:
                    if not isinstance(e, MalformedOutput):
                        breaker.on_failure(e)
                    else:
                        breaker.on_success()  # dependency answered; the content was bad
                    raise
                except Exception as e:  # unknown exceptions from tools are treated as transient
                    err = TransientError(f"{spec.dependency}: {type(e).__name__}: {e}", dependency=spec.dependency)
                    breaker.on_failure(err)
                    raise err from e
                finally:
                    attempts.append({"n": n, "ms": round((time.perf_counter() - t0) * 1000, 1)})
                breaker.on_success()
                return result

            def on_attempt(n, err, wait):
                if attempts and n < len(attempts):
                    attempts[n]["error"] = type(err).__name__ if err else None
                    attempts[n]["backoff_s"] = round(wait, 3)

            try:
                result = run_with_retry(attempt, policy=spec.retry if safe_to_retry else NO_RETRY,
                                        deadline=deadline, idempotent=safe_to_retry,
                                        on_attempt=on_attempt, sleep=self._sleep)
            finally:
                span.attrs["attempts"] = attempts
                span.attrs["breaker"] = breaker.state
            if idempotency_key and self.idem is not None:
                self.idem.put(f"{spec.dependency}:{idempotency_key}", result)
            return result


def _accepts_timeout(fn) -> bool:
    code = getattr(fn, "__code__", None) or getattr(getattr(fn, "__func__", None), "__code__", None)
    return bool(code and "timeout_s" in code.co_varnames[: code.co_argcount + code.co_kwonlyargcount])


def _corrupt(result: Any) -> Any:
    if isinstance(result, str):
        return result[: max(1, len(result) // 3)] + "<<truncated garbage"
    if hasattr(result, "text"):
        try:
            result.text = result.text[: max(1, len(result.text) // 3)] + "<<truncated garbage"
            if hasattr(result, "stop_reason"):
                result.stop_reason = "max_tokens"   # what a real cut-off completion reports
        except Exception:
            pass
    return result


def _short(v: Any, limit: int = 400) -> Any:
    s = json.dumps(v, default=str, ensure_ascii=False)
    return v if len(s) <= limit else s[:limit] + "…"
