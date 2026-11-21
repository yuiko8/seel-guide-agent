"""Deterministic MockLLM. Same interface as ClaudeClient, zero cost, zero network.
Uses the rule router / template writer so CI and offline evals are reproducible."""
from __future__ import annotations

import json

from .base import LLMResponse


class MockLLM:
    def __init__(self, model: str = "mock"):
        self.model = model

    def complete(self, *, task: str, system: str, messages: list[dict], json_schema: dict | None = None,
                 max_tokens: int = 4000, timeout_s: float = 20.0, payload: dict | None = None) -> LLMResponse:
        from ..agent.rules import rule_route, template_respond
        from ..harness.context import extractive_summary

        payload = payload or {}
        if task == "route":
            text = rule_route(payload["user_message"], payload.get("state")).model_dump_json()
        elif task == "respond":
            text = template_respond(payload["plan"])
        elif task == "summarize":
            text = extractive_summary(payload.get("prev", ""), payload["messages"])
        else:
            text = json.dumps({"note": f"mock has no handler for {task}"})
        in_tok = sum(len(m["content"]) for m in messages) // 4 + len(system) // 4
        return LLMResponse(text=text, model=self.model, input_tokens=in_tok, output_tokens=len(text) // 4)
