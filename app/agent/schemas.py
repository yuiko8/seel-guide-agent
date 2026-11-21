from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Intent = Literal["recommend", "policy_question", "protection_question", "shipping_question",
                 "order_issue", "smalltalk", "out_of_scope"]


class SlotUpdate(BaseModel):
    budget_max: float | None = None
    budget_min: float | None = None
    country: str | None = None
    recipient: str | None = None
    category: str | None = None
    keywords: list[str] = Field(default_factory=list)


class RouteDecision(BaseModel):
    intent: Intent
    slots: SlotUpdate = Field(default_factory=SlotUpdate)
    referenced_index: int | None = None     # "the second one" -> 2 (1-based, refers to last shown list)
    modifiers: list[Literal["cheaper", "more_premium", "different"]] = Field(default_factory=list)
    question: str | None = None             # standalone rewrite of a policy/shipping question
    injection_suspected: bool = False


def _nullable(t: str) -> dict:
    return {"anyOf": [{"type": t}, {"type": "null"}]}


ROUTE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["recommend", "policy_question", "protection_question", "shipping_question",
                                               "order_issue", "smalltalk", "out_of_scope"]},
        "slots": {
            "type": "object",
            "properties": {
                "budget_max": _nullable("number"),
                "budget_min": _nullable("number"),
                "country": _nullable("string"),
                "recipient": _nullable("string"),
                "category": {"anyOf": [{"type": "string", "enum": ["electronics", "apparel", "home", "beauty", "outdoor"]}, {"type": "null"}]},
                "keywords": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["budget_max", "budget_min", "country", "recipient", "category", "keywords"],
            "additionalProperties": False,
        },
        "referenced_index": _nullable("integer"),
        "modifiers": {"type": "array", "items": {"type": "string", "enum": ["cheaper", "more_premium", "different"]}},
        "question": _nullable("string"),
        "injection_suspected": {"type": "boolean"},
    },
    "required": ["intent", "slots", "referenced_index", "modifiers", "question", "injection_suspected"],
    "additionalProperties": False,
}
