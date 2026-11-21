from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

# USD per 1M tokens (input, output)
PRICING = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "mock": (0.0, 0.0),
    "rules": (0.0, 0.0),
}


@dataclass
class LLMResponse:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = "end_turn"
    request_id: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def cost_usd(self) -> float:
        pin, pout = PRICING.get(self.model, (0.0, 0.0))
        return (self.input_tokens * pin + self.output_tokens * pout) / 1_000_000


class LLMClient(Protocol):
    model: str

    def complete(self, *, task: str, system: str, messages: list[dict], json_schema: dict | None = None,
                 max_tokens: int = 4000, timeout_s: float = 20.0, payload: dict | None = None) -> LLMResponse: ...


def dependency_name(client: Any) -> str:
    return f"llm:{client.model}"
