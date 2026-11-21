"""LLM fallback chain: primary -> secondary -> (caller's rule/template fallback).

Per model: gateway call (breaker + timeout + transient retry). Malformed structured
output gets exactly ONE repair attempt on the same model (with the parse error fed
back) before moving to the next model. Each hop is recorded as a degradation.
"""
from __future__ import annotations

from typing import Any, Callable

from ..harness.deadline import Deadline
from ..harness.errors import DeadlineExceeded, HarnessError, MalformedOutput
from ..harness.gateway import CallSpec, Gateway
from ..harness.retry import RetryPolicy
from ..harness.tracing import Trace
from ..llm.base import LLMClient, LLMResponse


class AllModelsFailed(HarnessError):
    kind = "all_models_failed"


class LLMChain:
    def __init__(self, clients: list[LLMClient], gateway: Gateway, timeout_s: float = 20.0):
        self.clients = clients
        self.gateway = gateway
        self.timeout_s = timeout_s

    def call(self, *, task: str, system: str, messages: list[dict], trace: Trace, deadline: Deadline,
             json_schema: dict | None = None, payload: dict | None = None, max_tokens: int = 4000,
             parse: Callable[[str], Any] | None = None) -> tuple[LLMResponse, Any]:
        errors = []
        for i, client in enumerate(self.clients):
            spec = CallSpec(dependency=f"llm:{client.model}", timeout_s=self.timeout_s, idempotent=True,
                            retry=RetryPolicy(max_attempts=2, base_delay_s=0.5, max_delay_s=2.0))
            msgs = messages
            for repair in range(2):
                def validate(resp: LLMResponse):
                    trace.add_usage(resp.input_tokens, resp.output_tokens, resp.cost_usd, model=client.model)
                    if resp.stop_reason == "max_tokens":
                        # A cut-off draft is not a shorter answer: it can end mid-number ("total $4").
                        raise MalformedOutput(f"{task}: output truncated (max_tokens)", dependency=spec.dependency)
                    if parse is None:
                        return resp, resp.text
                    try:
                        return resp, parse(resp.text)
                    except Exception as e:  # noqa: BLE001
                        raise MalformedOutput(f"{task}: {type(e).__name__}: {str(e)[:200]}", dependency=spec.dependency)
                try:
                    resp, parsed = self.gateway.call(
                        spec, client.complete,
                        dict(task=task, system=system, messages=msgs, json_schema=json_schema,
                             max_tokens=max_tokens, payload=payload),
                        trace=trace, deadline=deadline, post_validate=validate,
                        span_attrs={"task": task, "model": client.model, "repair": bool(repair)})
                    if i > 0:
                        trace.degrade("llm_fallback_model", f"{task} served by {client.model}")
                    if repair:
                        trace.event("llm", "repair_succeeded", task=task, model=client.model)
                    return resp, parsed
                except MalformedOutput as e:
                    errors.append(f"{client.model}: {e}")
                    if repair == 0:
                        msgs = messages + [{"role": "assistant", "content": "(invalid output)"},
                                           {"role": "user", "content": f"Your previous output could not be used ({e}). "
                                                                       "Reply again, following the required format exactly."}]
                        trace.event("llm", "repair_attempt", status="error", task=task, model=client.model)
                        continue
                    break
                except DeadlineExceeded:
                    raise
                except HarnessError as e:
                    errors.append(f"{client.model}: {e.kind}: {e}")
                    break
        raise AllModelsFailed("; ".join(errors)[:500])
