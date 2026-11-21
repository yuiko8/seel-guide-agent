"""Rule-based router + template writer.

Two jobs:
  1. Deterministic backend for MockLLM (CI / offline evals).
  2. The last-resort degrade path when every LLM is down: the assistant keeps
     working (worse wording, same facts) instead of returning an error page.
"""
from __future__ import annotations

import re

from ..harness.guardrails import INJECTION
from ..harness.state import SessionState
from ..tools import kb
from ..tools.commerce import KEYWORD_MAP, TAG_WORDS
from .schemas import RouteDecision, SlotUpdate

RECIPIENTS = ["girlfriend", "boyfriend", "wife", "husband", "partner", "mom", "mother", "dad", "father", "sister",
              "brother", "friend", "colleague", "coworker", "boss", "son", "daughter", "kid", "grandma", "grandpa",
              "myself", "teen"]

ORDER_ISSUE = re.compile(r"\b(lost|never (arrived|came)|didn'?t (arrive|come)|hasn'?t arrived|not arrived|damaged|broken|"
                         r"stolen|where is my (order|package|parcel)|my (order|package|parcel)|tracking|refund me|"
                         r"want (a|my) refund|charged twice|wrong item)\b", re.I)
PROTECTION = re.compile(r"\b(protection|insurance|insure|assurance|protect|coverage|covered|claim)\b", re.I)
POLICY = re.compile(r"\b(return|returns|returnable|refund|exchange|warranty|final sale|restocking|send (it )?back|mail (it )?back|"
                    r"go back|postage|money back|defective|stopped working|rash|allergic)\b", re.I)
MERCHANT_ALIASES = {"nordlys": "nordlys", "maple": "maple", "maple & thread": "maple", "kiko": "kiko", "vela": "vela",
                    "trailhead": "trailhead"}
SHIPPING = re.compile(r"\b(how long|delivery|deliver|arrive|shipping|ship|customs|duty|duties|tax|taxes|vat|gst|ddp|import|"
                      r"track|tracking|address|courier)\b", re.I)
SHIP_QUESTION = re.compile(r"\b(how long|how much|when will|customs|duty|duties|tax|taxes|vat|gst|ddp|can (you|it|they) ship|"
                           r"do you ship|does it ship|will it arrive)\b|\?", re.I)
RECOMMEND = re.compile(r"\b(recommend|suggest|looking for|look for|want|need|gift|present|buy|get (her|him|them|me)|budget|"
                       r"ideas?|options?|show me|find|something|cheaper|less expensive|alternatives?|more premium|nicer)\b", re.I)
SMALLTALK = re.compile(r"^\s*(hi|hello|hey|thanks|thank you|ok|okay|cool|great|bye)\b[\s!.]*$", re.I)
ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "last": -1}

MONEY = r"\$?\s?(\d{1,6}(?:\.\d{1,2})?)\s?(?:usd|dollars|bucks|\$)?"


def parse_budget(text: str) -> tuple[float | None, float | None]:
    t = text.lower().replace(",", "")
    m = re.search(rf"between\s+{MONEY}\s+(?:and|-|to)\s+{MONEY}", t)
    if m:
        return float(m.group(2)), float(m.group(1))
    m = re.search(rf"\b{MONEY}\s?(?:-|to)\s?{MONEY}\b", t)
    if m and ("$" in t or "budget" in t):
        return float(m.group(2)), float(m.group(1))
    m = re.search(rf"(?:under|below|less than|max(?:imum)?|up to|within|no more than|budget(?: is| of)?|around|about|<)\s*{MONEY}", t)
    if m:
        v = float(m.group(1))
        if "around" in m.group(0) or "about" in m.group(0):
            return round(v * 1.15, 2), round(v * 0.7, 2)
        return v, None
    m = re.search(r"\$\s?(\d{1,6}(?:\.\d{1,2})?)", t)
    if m and not re.search(r"\b(cost|costs|paid|charged|is it|was)\b", t):
        return float(m.group(1)), None
    m = re.search(r"(\d{2,5})\s?(?:usd|dollars|bucks)\b", t)
    if m:
        return float(m.group(1)), None
    return None, None


def parse_country(text: str) -> str | None:
    low = " " + re.sub(r"[^\w\s]", " ", text.lower()) + " "
    # multi-word names first
    for alias in sorted(kb.COUNTRY_ALIASES, key=len, reverse=True):
        if len(alias) <= 2:
            continue
        if f" {alias} " in low:
            return kb.COUNTRY_ALIASES[alias]
    # 2-letter codes only when written in capitals ("US", "UK", "DE") to avoid "us" the pronoun
    for code in re.findall(r"\b([A-Z]{2})\b", text):
        c = kb.normalize_country(code) if code != "UK" else "GB"
        if c and code not in ("OK", "TV", "PC", "ID"):
            return c
    return None


def parse_keywords(text: str) -> tuple[list[str], str | None]:
    low = text.lower()
    kws, cat = [], None
    for k in sorted(KEYWORD_MAP, key=len, reverse=True):
        if re.search(rf"\b{re.escape(k)}s?\b", low) and not any(k in x for x in kws):
            kws.append(k)
            cat = cat or KEYWORD_MAP[k][0]
    for t in TAG_WORDS:
        if re.search(rf"\b{re.escape(t)}\b", low) and t not in kws:
            kws.append(t)
    if not cat:
        for c, words in {"electronics": ["tech", "gadget", "electronic", "audio", "music"],
                         "apparel": ["clothes", "clothing", "fashion", "wear", "outfit"],
                         "home": ["home decor", "kitchen", "cozy", "house"],
                         "beauty": ["beauty", "makeup", "cosmetic"],
                         "outdoor": ["hiking", "camping gear", "outdoorsy", "hiker"]}.items():
            if any(re.search(rf"\b{w}\b", low) for w in words):
                cat = c
                break
    return kws, cat


def rule_route(text: str, state: SessionState | None = None) -> RouteDecision:
    bmax, bmin = parse_budget(text)
    kws, cat = parse_keywords(text)
    recipient = next((r for r in RECIPIENTS if re.search(rf"\b{r}\b", text.lower())), None)
    slots = SlotUpdate(budget_max=bmax, budget_min=bmin, country=parse_country(text), recipient=recipient,
                       category=cat, keywords=[k for k in kws if k != "gift"])
    low = text.lower()
    modifiers = []
    if re.search(r"\b(cheaper|less expensive|lower price|too expensive|more affordable)\b", low):
        modifiers.append("cheaper")
    if re.search(r"\b(more premium|nicer|higher end|fancier|better quality)\b", low):
        modifiers.append("more_premium")
    if re.search(r"\b(something else|other options|different|alternatives?)\b", low):
        modifiers.append("different")
    ref = None
    m = re.search(r"\b(first|1st|second|2nd|third|3rd|last)\b(?: one| option| item| pick)?", low)
    if m and state and state.candidates:
        ref = ORDINALS[m.group(1)]
    m2 = re.search(r"\b(?:#|number |option )(\d)\b", low)
    if m2 and state and state.candidates:
        ref = int(m2.group(1))

    has_product_signal = bool(kws or cat or bmax or recipient or modifiers)
    hypothetical = re.search(r"\b(policy|what does .{1,30} say|what if|what happens if|in case|would|if (it|the|my) )", low)
    if ORDER_ISSUE.search(text) and not RECOMMEND.search(text) and not hypothetical:
        intent = "order_issue"
    elif PROTECTION.search(text):
        intent = "protection_question"
    elif hypothetical and ORDER_ISSUE.search(text):
        has_store = any(re.search(rf"\b{re.escape(a)}\b", low) for a in MERCHANT_ALIASES)
        intent = "policy_question" if (has_store or kws) else "protection_question"
    elif POLICY.search(text) and not (has_product_signal and RECOMMEND.search(text)):
        intent = "policy_question"
    elif SHIPPING.search(text) and (ref or SHIP_QUESTION.search(text) or not (kws or cat or recipient or bmax)):
        intent = "shipping_question"
    elif RECOMMEND.search(text) or has_product_signal or slots.country:
        intent = "recommend"
    elif SMALLTALK.search(text):
        intent = "smalltalk"
    else:
        intent = "out_of_scope"
    q = text.strip() if intent in ("policy_question", "shipping_question", "protection_question") else None
    merchant = next((m for a, m in MERCHANT_ALIASES.items() if re.search(rf"\b{re.escape(a)}\b", low)), None)
    return RouteDecision(intent=intent, slots=slots, referenced_index=ref, modifiers=modifiers, question=q,
                         merchant=merchant, injection_suspected=bool(INJECTION.search(text)))


# ---------------------------------------------------------------------------
# Template writer
# ---------------------------------------------------------------------------

def _first_sentences(text: str, n: int) -> str:
    text = re.sub(r"^- ", "", text.strip(), flags=re.M).replace("\n", " ")
    parts = re.split(r"(?<=[.!?])\s+", text)
    return " ".join(parts[:n]).strip()


def _money(v: float) -> str:
    return f"${v:,.2f}"


def template_respond(plan: dict) -> str:
    t = plan["type"]
    if t == "recommend":
        s = plan["slots"]
        who = f" for your {s['recipient']}" if s.get("recipient") else ""
        budget = f" under {_money(s['budget_max'])}" if s.get("budget_max") else ""
        where = f" that ship to {kb.country_name(s.get('country'))}" if s.get("country") else ""
        lines = [f"Here are {len(plan['products'])} picks{who}{budget}{where}:"]
        for i, p in enumerate(plan["products"], 1):
            sh = p.get("shipping")
            line = f"{i}. {p['title']} from {p['merchant']} — {_money(p['price_usd'])} ({p['rating']}★)."
            if sh and sh.get("shippable"):
                extra = sh["tax_usd"] + sh["duty_usd"]
                line += f" Arrives in {sh['standard_days'][0]}–{sh['standard_days'][1]} days"
                if extra > 0:
                    line += f"; about {_money(extra)} in {sh['tax_name']}/duties, collected {sh['taxes_collected']}"
                line += f". Estimated total {_money(sh['landed_total_usd'])}."
            lines.append(line)
        if not plan["slots"].get("country"):
            lines.append("Tell me which country it ships to and I'll add delivery time and taxes.")
        else:
            lines.append("Want me to compare two of these, or check the return policy?")
        return "\n".join(lines)
    if t == "clarify":
        return plan["question"]
    if t == "no_results":
        f = plan.get("filters", {})
        tips = []
        if f.get("budget_max"):
            tips.append("raise the budget a little")
        if f.get("subcategories") or f.get("categories"):
            tips.append("try a different kind of item")
        msg = "I couldn't find anything that matches all of that"
        if plan.get("restricted_examples"):
            msg += f" — some items can't be shipped there ({plan['restricted_examples'][0]['reason']})"
        return msg + ". " + (f"Could you {' or '.join(tips)}?" if tips else "Could you tell me a bit more about what you're after?")
    if t == "shipping_answer":
        if plan.get("unsupported"):
            return plan["unsupported"] + " Is there another address in a different country I can check for you?"
        g = plan.get("general") or {}
        lines = []
        for it in plan.get("items", []):
            sh = it.get("shipping")
            if not sh:
                lines.append(f"- {it['title']}: delivery estimate is temporarily unavailable.")
            elif not sh.get("shippable"):
                lines.append(f"- {it['title']}: can't ship there — {sh.get('reason')}")
            else:
                extra = sh["tax_usd"] + sh["duty_usd"]
                lines.append(f"- {it['title']}: {sh['standard_days'][0]}–{sh['standard_days'][1]} days, "
                             f"about {_money(extra)} in {sh['tax_name']}/duties, paid {sh['taxes_collected']}, "
                             f"estimated total {_money(sh['landed_total_usd'])}.")
        head = f"Shipping to {g.get('country_name') or kb.country_name(plan.get('country'))}"
        if g.get("standard_days"):
            head += f" usually takes {g['standard_days'][0]}–{g['standard_days'][1]} days"
        if g.get("tax_name") and g.get("tax_rate_pct"):
            head += f", and {g['tax_name']} of {g['tax_rate_pct']}% applies"
        head += "."
        if g.get("ddp") is True:
            head += " Duties and taxes are paid at checkout (DDP), so nothing is due on delivery."
        elif g.get("ddp") is False:
            head += " DDP isn't available there, so the courier collects duties and taxes from the recipient on delivery."
        for x in (plan.get("sources") or [])[:1]:
            if "ddp" in x["id"] and g.get("ddp") is not None:
                continue  # already explained concretely for this country
            head += f" {_first_sentences((plan.get('_source_texts') or {}).get(x['n'], ''), 1)} [{x['n']}]"
        return head + ("\n" + "\n".join(lines) if lines else "")
    if t == "policy_answer":
        texts = plan.get("_source_texts") or {}
        srcs = plan.get("sources") or []
        store = [x for x in srcs if x["id"].startswith("policy:")]
        rest = [x for x in srcs if not x["id"].startswith("policy:")]
        ra = next((x for x in rest if x["id"] == "protection#return-assurance"), None)
        # degrade path shows top-2: retrieval R@1 is not reliable enough to trust a single chunk
        main = store[:2] if store else rest[:2]
        head = f"Here's what {plan['merchant']}'s policy says:" if store and plan.get("merchant") else "Here's what I found:"
        lines = [head] + [f"• {x['section']}: {_first_sentences(texts.get(x['n'], ''), 2)} [{x['n']}]" for x in main]
        if store and ra:
            lines.append(f"• If Return Assurance was added at checkout: {_first_sentences(texts.get(ra['n'], ''), 1)} [{ra['n']}]")
        return "\n".join(lines)
    if t == "smalltalk":
        return "Hi! I can help you find a gift or product, check delivery times and import taxes to your country, and explain return policies. What are you shopping for?"
    if t == "out_of_scope":
        return "I'm a shopping assistant, so I can help with finding products, delivery times, taxes and duties, returns, and purchase protection. What can I help you find?"
    if t == "unsupported_yet":
        return plan.get("message") or "I can't help with that here yet. Let me connect you with our support team."
    if t == "injection":
        return "I can only help with shopping questions, and I can't change prices or rules. What are you looking for today?"
    if t == "degraded":
        return plan.get("message") or "Sorry — I'm having trouble right now. Please try again in a moment, or I can connect you with our support team."
    return plan.get("message", "How can I help with your shopping today?")
