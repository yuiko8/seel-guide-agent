"""Input and output guardrails.

Output checks run on every LLM-written reply before it reaches the shopper:
  - unsupported_number : a $ amount that is not derivable from FACTS (hallucinated price/tax)
  - unsafe_promise     : promising a refund / claim approval / delivery date guarantee
  - internal_leak      : mentions of tools, system prompt, JSON, "FACTS"
A failing draft gets one regeneration with the violations as feedback; a second
failure falls back to the deterministic template (facts only), which is always safe.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_INPUT_CHARS = 2000

PRICE_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)")
PROMISES = [
    re.compile(p, re.I) for p in [
        r"\b(you will|you'll|we will|we'll)\s+(definitely\s+|certainly\s+|surely\s+)?(get|receive|be given|issue)\s+(you\s+)?(a\s+)?(full\s+)?refund",
        r"\bguarantee(d|s)?\b.{0,40}\b(refund|approv|deliver|arrive)",
        r"\b(claim|refund)\b.{0,30}\b(will be|is|has been)\s+approved",
        r"\b(will|is going to)\s+(definitely|certainly)\s+arrive",
        r"\bi (have|'ve) (issued|processed|approved) (a|your) refund",
    ]
]
LEAKS = re.compile(r"\b(system prompt|FACTS block|json|tool call|function call|as an ai language model|\[(product|shipping|quote|policy_chunk):)", re.I)
INJECTION = re.compile(r"ignore (all |any |the )?(previous|prior|above) (instructions|rules)|system prompt|developer mode|"
                       r"you are now|pretend (you are|to be)|jailbreak|reveal your (prompt|instructions)", re.I)


@dataclass
class GuardResult:
    ok: bool
    violations: list[dict] = field(default_factory=list)


def check_input(text: str) -> tuple[str, list[str]]:
    flags = []
    if len(text) > MAX_INPUT_CHARS:
        text = text[:MAX_INPUT_CHARS]
        flags.append("truncated")
    if INJECTION.search(text):
        flags.append("injection_suspected")
    return text, flags


def allowed_amounts(facts_numbers: list[float]) -> set[float]:
    allowed: set[float] = set()
    for v in facts_numbers:
        if v is None:
            continue
        v = float(v)
        allowed.update({round(v, 2), float(round(v)), float(int(v))})
    return allowed


def check_output(text: str, facts_numbers: list[float]) -> GuardResult:
    violations = []
    allowed = allowed_amounts(facts_numbers)
    for m in PRICE_RE.finditer(text):
        val = float(m.group(1).replace(",", ""))
        if not any(abs(val - a) <= 0.011 for a in allowed):
            violations.append({"type": "unsupported_number", "value": m.group(0)})
    for p in PROMISES:
        m = p.search(text)
        if m:
            violations.append({"type": "unsafe_promise", "text": m.group(0)})
    m = LEAKS.search(text)
    if m:
        violations.append({"type": "internal_leak", "text": m.group(0)})
    return GuardResult(ok=not violations, violations=violations)


def numbers_in(obj) -> list[float]:
    """Collect every number in a facts structure (prices, taxes, totals...)."""
    out: list[float] = []
    if isinstance(obj, bool):
        return out
    if isinstance(obj, (int, float)):
        out.append(float(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            out.extend(numbers_in(v))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out.extend(numbers_in(v))
    return out


# ---------------------------------------------------------------------------
# Grounding check for answers built from retrieved sources (policy / protection / FAQ)
# ---------------------------------------------------------------------------
CITE_RE = re.compile(r"\[(?:source:)?(\d{1,2})\]")
CLAIM_NUM_RE = re.compile(r"(\$\s?\d+(?:\.\d{1,2})?|\d+(?:\.\d+)?\s?(?:%|percent|days?|hours?|business days|years?|months?|weeks?|kg))", re.I)


def normalize_citations(text: str) -> str:
    return CITE_RE.sub(lambda m: f"[{m.group(1)}]", text)


def check_grounding(text: str, sources: dict[int, str]) -> GuardResult:
    """sources: citation number -> source text.
    - uncited_answer   : a sourced answer with no [n] markers
    - bad_citation     : [n] that doesn't exist
    - ungrounded_claim : a number+unit ("45 days", "$12", "20%") that appears in no source text
    """
    v = []
    cites = {int(n) for n in CITE_RE.findall(text)}
    if not cites:
        v.append({"type": "uncited_answer"})
    for n in sorted(cites - set(sources)):
        v.append({"type": "bad_citation", "value": f"[{n}]"})
    corpus = " ".join(sources.values()).lower()
    corpus_nums = set(re.findall(r"\d+(?:\.\d+)?", corpus))
    for m in CLAIM_NUM_RE.finditer(text):
        num = re.search(r"\d+(?:\.\d+)?", m.group(0)).group(0)
        if num not in corpus_nums:
            v.append({"type": "ungrounded_claim", "value": m.group(0)})
    return GuardResult(ok=not v, violations=v)
