"""Turn the knowledge base into retrievable chunks with stable ids.

Chunk id = "<doc_id>#<section-slug>", e.g. "policy:maple#final-sale". Ids are what
answers cite and what retrieval evals score against, so they must be stable.
Sources:
  - merchant policies (markdown, split by '## ' sections)
  - protection terms + FAQ (markdown)
  - shipping_rules.json rendered into prose per country / restriction
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..tools import kb


@dataclass
class Chunk:
    id: str
    doc_id: str
    doc_title: str
    section: str
    text: str
    meta: dict = field(default_factory=dict)


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def split_markdown(doc_id: str, md: str, meta: dict) -> list[Chunk]:
    title_m = re.search(r"^# (.+)$", md, re.M)
    title = title_m.group(1).strip() if title_m else doc_id
    parts = re.split(r"^## ", md, flags=re.M)
    intro = parts[0].split("\n", 1)[1].strip() if "\n" in parts[0] else ""
    chunks = []
    if intro:
        chunks.append(Chunk(f"{doc_id}#overview", doc_id, title, "Overview", intro, meta))
    for p in parts[1:]:
        head, _, body = p.partition("\n")
        chunks.append(Chunk(f"{doc_id}#{slug(head)}", doc_id, title, head.strip(), body.strip(), dict(meta)))
    return chunks


def shipping_chunks() -> list[Chunk]:
    r = kb.shipping_rules()
    out = []
    for code, c in r["countries"].items():
        tax = f"{c['tax_name']} of {round(c['tax_rate'] * 100)}%" if c["tax_rate"] else "no import tax"
        thr = (f"Duties apply to orders over ${c['duty_free_threshold_usd']}." if c["duty_free_threshold_usd"]
               else "There is no duty-free threshold.")
        ddp = ("Duties and taxes are collected at checkout (DDP), so nothing is due on delivery." if c["ddp_available"]
               else "DDP is not available: the courier collects duties and taxes from the recipient on delivery.")
        text = (f"Shipping from the US to {c['name']} takes {c['standard_days'][0]}–{c['standard_days'][1]} days with standard "
                f"shipping and {c['express_days'][0]}–{c['express_days'][1]} days with express. Imports are subject to {tax}. "
                f"{thr} {ddp} Peak-season delay risk: {c['peak_delay_risk']}.")
        out.append(Chunk(f"shipping:{code}#overview", f"shipping:{code}", "Cross-border shipping rules (simulated)",
                         c["name"], text, {"country": code, "doc_type": "shipping"}))
    for code, reason in r["unsupported_destinations"].items():
        out.append(Chunk(f"shipping:{code}#unsupported", f"shipping:{code}", "Cross-border shipping rules (simulated)",
                         kb.country_name(code), reason, {"country": code, "doc_type": "shipping"}))
    for rule in r["restrictions"]:
        names = ", ".join(kb.country_name(c) for c in rule["countries"])
        out.append(Chunk(f"shipping:restriction#{rule['id'].lower()}", "shipping:restriction",
                         "Shipping restrictions (simulated)", rule["id"], f"{rule['rule']} Applies to: {names}.",
                         {"doc_type": "restriction"}))
    pk = r["peak_season"]
    out.append(Chunk("shipping:peak#overview", "shipping:peak", "Cross-border shipping rules (simulated)", "Peak season",
                     f"During peak season (November and December) international deliveries take an extra "
                     f"{pk['extra_days'][0]}–{pk['extra_days'][1]} days.", {"doc_type": "shipping"}))
    return out


def build_chunks() -> list[Chunk]:
    chunks: list[Chunk] = []
    for mid, m in kb.merchants().items():
        md = (kb.DATA / "policies" / f"{mid}.md").read_text()
        chunks += split_markdown(f"policy:{mid}", md, {"merchant_id": mid, "merchant": m["name"], "doc_type": "policy"})
    chunks += split_markdown("protection", (kb.DATA / "protection_terms.md").read_text(), {"doc_type": "protection"})
    chunks += split_markdown("faq", (kb.DATA / "faq.md").read_text(), {"doc_type": "faq"})
    chunks += shipping_chunks()
    return chunks
