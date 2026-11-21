"""Context manager: decides exactly what the LLM sees on each call.

Budget partitions (approx tokens, chars/4):
  system   - fixed instructions (cache-friendly, never trimmed)
  slots    - structured user profile; ALWAYS included, never trimmed
  summary  - compressed older history
  facts    - tool results / retrieved chunks, ranked; lowest-ranked dropped first
  history  - most recent turns, newest kept first

Overflow order: drop low-ranked facts -> drop oldest history (which gets folded into
the summary by `compact()` at the end of the turn). Slots are the anchor that
prevents drift ("ship to Germany" said on turn 3 is still known on turn 20).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

from .state import Message, SessionState


def approx_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


@dataclass
class ContextBudget:
    total: int = 6000
    summary: int = 400
    facts: int = 2500
    history: int = 1800
    compact_history_over: int = 1800   # compact when un-summarized history exceeds this
    keep_recent_turns: int = 4         # never fold the last N turns into the summary


@dataclass
class Fact:
    kind: str           # product | shipping | policy_chunk | quote | restriction | note
    ref: str            # stable id used for citations: sku / doc#chunk
    data: dict
    priority: float = 1.0


@dataclass
class ContextPack:
    system: str
    messages: list[dict]
    stats: dict = field(default_factory=dict)
    facts_included: list[str] = field(default_factory=list)


class ContextManager:
    def __init__(self, budget: ContextBudget | None = None):
        self.budget = budget or ContextBudget()

    def slots_block(self, state: SessionState) -> str:
        filled = state.slots.filled()
        return "KNOWN SHOPPER PROFILE (authoritative, keep using it):\n" + (json.dumps(filled) if filled else "{}")

    def build(self, state: SessionState, system: str, task_block: str, facts: list[Fact]) -> ContextPack:
        b = self.budget
        stats: dict = {"budget_total": b.total}
        slots = self.slots_block(state)
        used = approx_tokens(system) + approx_tokens(slots) + approx_tokens(task_block)

        # summary
        summary = state.summary
        if approx_tokens(summary) > b.summary:
            summary = summary[-b.summary * 4:]
            stats["summary_truncated"] = True
        used += approx_tokens(summary)

        # facts: highest priority first, stop at budget
        fact_lines, included, dropped = [], [], 0
        f_used = 0
        for f in sorted(facts, key=lambda x: -x.priority):
            line = f"[{f.kind}:{f.ref}] {json.dumps(f.data, ensure_ascii=False)}"
            t = approx_tokens(line)
            if f_used + t > b.facts or used + f_used + t > b.total - 200:
                dropped += 1
                continue
            fact_lines.append(line)
            included.append(f.ref)
            f_used += t
        used += f_used

        # history: newest first, only turns not already folded into the summary
        hist = [m for m in state.messages if m.turn > state.summarized_upto_turn]
        kept: list[Message] = []
        h_used = 0
        for m in reversed(hist):
            t = approx_tokens(m.content)
            if h_used + t > b.history or used + h_used + t > b.total:
                break
            kept.append(m)
            h_used += t
        kept.reverse()
        used += h_used

        # Messages API needs alternating roles starting with user; merge facts+task into final user msg.
        msgs: list[dict] = []
        for m in kept:
            if msgs and msgs[-1]["role"] == m.role:
                msgs[-1]["content"] += "\n" + m.content
            else:
                msgs.append({"role": m.role, "content": m.content})
        if msgs and msgs[0]["role"] == "assistant":
            msgs.insert(0, {"role": "user", "content": "(conversation continues)"})
        context_block = "\n\n".join(x for x in [
            slots,
            f"EARLIER CONVERSATION SUMMARY:\n{summary}" if summary else "",
            "FACTS (only source of truth for prices, delivery times, taxes, policies):\n" + "\n".join(fact_lines) if fact_lines else "FACTS: none",
            task_block,
        ] if x)
        if msgs and msgs[-1]["role"] == "user":
            msgs[-1]["content"] = context_block + "\n\nSHOPPER'S LATEST MESSAGE:\n" + msgs[-1]["content"]
        else:
            msgs.append({"role": "user", "content": context_block})

        stats.update({
            "tokens_est": used, "facts_included": len(included), "facts_dropped": dropped,
            "history_turns_kept": len({m.turn for m in kept}), "history_turns_total": len({m.turn for m in hist}),
            "summary_tokens": approx_tokens(summary) if summary else 0,
        })
        return ContextPack(system=system, messages=msgs, stats=stats, facts_included=included)

    # ---- compaction -----------------------------------------------------------------
    def needs_compaction(self, state: SessionState) -> bool:
        hist = [m for m in state.messages if m.turn > state.summarized_upto_turn]
        return sum(approx_tokens(m.content) for m in hist) > self.budget.compact_history_over

    def compact(self, state: SessionState, summarize: Callable[[str, list[Message]], str]) -> dict:
        """Fold all but the last `keep_recent_turns` turns into state.summary."""
        cutoff = state.turn - self.budget.keep_recent_turns
        if cutoff <= state.summarized_upto_turn:
            return {"compacted": False}
        to_fold = [m for m in state.messages if state.summarized_upto_turn < m.turn <= cutoff]
        if not to_fold:
            return {"compacted": False}
        new_summary = summarize(state.summary, to_fold)
        state.summary = new_summary[-self.budget.summary * 4:]  # keep the most recent part
        state.summarized_upto_turn = cutoff
        return {"compacted": True, "folded_messages": len(to_fold), "upto_turn": cutoff,
                "summary_tokens": approx_tokens(state.summary)}


def extractive_summary(prev: str, msgs: list[Message]) -> str:
    """Deterministic fallback summarizer (used by MockLLM and when the LLM is degraded)."""
    lines = [prev] if prev else []
    for m in msgs:
        if m.role == "user":
            lines.append(f"T{m.turn} shopper: {m.content[:140]}")
        else:
            lines.append(f"T{m.turn} assistant: {m.content.split('.')[0][:100]}")
    return "\n".join(lines)
