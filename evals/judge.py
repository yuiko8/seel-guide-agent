"""LLM-as-judge for conversation replies (optional; needs ANTHROPIC_API_KEY or an injected client).

Rule checks in evals/checks.py decide pass/fail and the gate. The judge adds graded, explained scores for
what rules can't see (helpfulness, honesty about limits) and for labelled cases whose expectation is
free text ("should suggest earbuds"). Scores are reported, never gated: a judge is itself a model and drifts.

The judge sees only the user turns, the reply, the structured facts the app rendered, and the expected
behaviour; it never sees the agent's prompts.
"""
from __future__ import annotations

import json
import statistics

from app.harness.circuit_breaker import BreakerRegistry
from app.harness.deadline import Deadline
from app.harness.gateway import CallSpec, Gateway
from app.harness.retry import RetryPolicy
from app.harness.tracing import Trace

JUDGE_MODEL = "claude-sonnet-5-5"
JUDGE_SYSTEM = """You grade replies from a cross-border shopping assistant. The store's facts (prices, taxes,
delivery windows, policy text, protection quotes) are provided as FACTS; the reply must not contradict them,
invent numbers, or promise refunds, claim approvals or delivery dates. Grade 1-5 on each criterion:
- factuality: every concrete claim is supported by FACTS (5 = fully supported, 1 = invents or contradicts)
- helpfulness: answers what the shopper actually asked and moves them forward
- boundaries: is honest about what it can't do / doesn't know, and about what protection does NOT cover
- expectation: meets EXPECTED BEHAVIOUR if one is given (use 5 if none is given)
Be strict and brief. Return JSON only."""
JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "factuality": {"type": "integer"}, "helpfulness": {"type": "integer"},
        "boundaries": {"type": "integer"}, "expectation": {"type": "integer"},
        "reason": {"type": "string"},
    },
    "required": ["factuality", "helpfulness", "boundaries", "expectation", "reason"],
    "additionalProperties": False,
}
CRITERIA = ("factuality", "helpfulness", "boundaries", "expectation")


def _client():
    from app.llm.claude import ClaudeClient
    return ClaudeClient(JUDGE_MODEL)


def judge_reply(client, gateway: Gateway, user_turns: list[str], reply: str, facts: dict, expected: str = "") -> dict:
    trace = Trace("judge", 0, "judge")
    prompt = (f"CONVERSATION (user turns):\n{json.dumps(user_turns, ensure_ascii=False)}\n\n"
              f"FACTS:\n{json.dumps(facts, ensure_ascii=False)[:6000]}\n\n"
              f"EXPECTED BEHAVIOUR:\n{expected or '(none)'}\n\nREPLY TO GRADE:\n{reply}")
    resp = gateway.call(
        CallSpec(dependency=f"llm:{client.model}", timeout_s=30, retry=RetryPolicy(max_attempts=2)),
        client.complete,
        dict(task="judge", system=JUDGE_SYSTEM, messages=[{"role": "user", "content": prompt}],
             json_schema=JUDGE_SCHEMA, max_tokens=800),
        trace=trace, deadline=Deadline(60))
    out = json.loads(resp.text)
    for k in CRITERIA:
        out[k] = max(1, min(5, int(out[k])))
    return out | {"cost_usd": trace.usage["cost_usd"] or resp.cost_usd}


def judge_results(results, cases_by_id: dict, client=None, max_cases: int = 80) -> dict:
    """Judge the last turn of each case. `results` are CaseResult objects from evals.run."""
    client = client or _client()
    gw = Gateway(BreakerRegistry())
    scores, items = {k: [] for k in CRITERIA}, []
    for r in results[:max_cases]:
        if not r.turns:
            continue
        t = r.turns[-1]
        case = cases_by_id.get(r.id, {})
        facts = {"slots": t.slots, "ui_blocks": t.structured, "degraded": t.degraded}
        try:
            j = judge_reply(client, gw, [x.message for x in r.turns], t.reply, facts,
                            case.get("behavior") or case.get("note", ""))
        except Exception as e:  # noqa: BLE001 - judge outages must not break the eval
            items.append({"id": r.id, "error": f"{type(e).__name__}: {e}"[:200]})
            continue
        r.judge = j
        items.append({"id": r.id, **j})
        for k in CRITERIA:
            scores[k].append(j[k])
    return {"model": getattr(client, "model", "?"), "n": len(scores["factuality"]),
            "means": {k: round(statistics.mean(v), 2) if v else None for k, v in scores.items()},
            "lowest": sorted((i for i in items if "factuality" in i), key=lambda i: sum(i[k] for k in CRITERIA))[:10],
            "errors": [i for i in items if "error" in i]}
