"""Claude client. SDK-level retries are disabled (max_retries=0) on purpose: the
harness gateway owns retry/backoff/breaker decisions so they are visible in traces
and bounded by the turn deadline. SDK exceptions are mapped onto the harness
error taxonomy here."""
from __future__ import annotations

import os

import anthropic

from ..harness.errors import DependencyTimeout, MalformedOutput, PermanentError, RateLimited, Refused, TransientError
from .base import LLMResponse


class ClaudeClient:
    def __init__(self, model: str = "claude-opus-5-5", effort: str = "low", server_fallbacks: bool | None = None):
        self.model = model
        self.effort = effort
        self._client = anthropic.Anthropic(max_retries=0)
        if server_fallbacks is None:
            server_fallbacks = os.getenv("ANTHROPIC_SERVER_FALLBACKS", "1") == "1"
        self.server_fallbacks = server_fallbacks

    def complete(self, *, task: str, system: str, messages: list[dict], json_schema: dict | None = None,
                 max_tokens: int = 4000, timeout_s: float = 20.0, payload: dict | None = None) -> LLMResponse:
        output_config: dict = {"effort": self.effort}
        if json_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": json_schema}
        kwargs = dict(model=self.model, max_tokens=max_tokens, system=system, messages=messages,
                      output_config=output_config)
        dep = f"llm:{self.model}"
        try:
            client = self._client.with_options(timeout=timeout_s, max_retries=0)
            if self.server_fallbacks:
                # On a safety-classifier decline the API re-runs the request on a fallback model.
                resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
            else:
                resp = client.messages.create(**kwargs)
        except anthropic.APITimeoutError as e:
            raise DependencyTimeout(str(e), dependency=dep) from e
        except anthropic.RateLimitError as e:
            ra = e.response.headers.get("retry-after") if e.response is not None else None
            raise RateLimited(str(e), dependency=dep, retry_after=float(ra) if ra else None) from e
        except anthropic.APIStatusError as e:
            if e.status_code >= 500 or e.status_code in (408, 409):
                raise TransientError(f"{e.status_code}: {e.message}", dependency=dep) from e
            raise PermanentError(f"{e.status_code}: {e.message}", dependency=dep) from e
        except anthropic.APIConnectionError as e:
            raise TransientError(f"connection: {e}", dependency=dep) from e

        if resp.stop_reason == "refusal":
            raise Refused(f"refusal ({getattr(resp.stop_details, 'category', None)})", dependency=dep)
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        if resp.stop_reason == "max_tokens" and json_schema is not None:
            raise MalformedOutput("truncated structured output (max_tokens)", dependency=dep)
        if not text.strip():
            raise MalformedOutput("empty response", dependency=dep)
        return LLMResponse(text=text, model=resp.model or self.model, input_tokens=resp.usage.input_tokens,
                           output_tokens=resp.usage.output_tokens, stop_reason=resp.stop_reason or "end_turn",
                           request_id=getattr(resp, "_request_id", None))
