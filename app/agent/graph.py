"""Agent runtime: an explicit state machine driven by the harness.

Turn lifecycle
  load state (or create) -> request-id idempotency check -> run nodes (bounded steps,
  turn deadline) -> persist with optimistic lock -> record snapshot + trace.

Nodes (each returns the name of the next node):
  intake -> route -> {clarify | recommend | shipping_qa | static} -> respond -> finalize

Any unexpected exception or deadline expiry inside the node loop jumps to a
degraded reply, so the shopper always gets a coherent answer and the trace says why.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings
from ..harness.circuit_breaker import BreakerConfig, BreakerRegistry
from ..harness.context import ContextManager, Fact, extractive_summary
from ..harness.deadline import Deadline
from ..harness.errors import CircuitOpen, DeadlineExceeded, HarnessError
from ..harness.faults import FaultInjector
from ..harness.gateway import CallSpec, Gateway
from ..harness.guardrails import check_grounding, check_input, check_output, normalize_citations, numbers_in
from ..harness.retry import RetryPolicy
from ..harness.state import ConcurrentModification, Message, SessionState, SessionStore
from ..harness.tracing import Trace, write_jsonl
from ..llm.base import LLMClient
from ..rag.retriever import HybridRetriever, default_retriever
from ..tools import commerce, kb
from ..tools.protection import QuoteInput, quote_protection
from ..tools.tickets import TicketInput, TicketService, classify_issue, extract_order_id
from . import prompts
from .llm_chain import AllModelsFailed, LLMChain
from .rules import rule_route, template_respond
from .schemas import ROUTE_JSON_SCHEMA, RouteDecision

DETERMINISTIC_PLANS = {"clarify", "smalltalk", "out_of_scope", "injection", "unsupported_yet", "degraded", "no_answer"}


@dataclass
class Turn:
    state: SessionState
    user_message: str
    trace: Trace
    deadline: Deadline
    flags: list[str] = field(default_factory=list)
    route: RouteDecision | None = None
    plan: dict | None = None
    facts: list[Fact] = field(default_factory=list)
    cards: list[dict] = field(default_factory=list)
    chips: list[str] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)   # numbered, citable retrieved chunks
    blocks: list[dict] = field(default_factory=list)    # extra UI blocks: protection_quote, ticket
    reply: str = ""


class Agent:
    def __init__(self, settings: Settings, llms: list[LLMClient], store: SessionStore | None = None,
                 faults: FaultInjector | None = None, breakers: BreakerRegistry | None = None,
                 context: ContextManager | None = None, retriever: HybridRetriever | None = None):
        self.s = settings
        self.retriever = retriever or default_retriever()
        self.store = store or SessionStore(settings.db_path)
        self.breakers = breakers or BreakerRegistry(
            BreakerConfig(window=20, min_calls=5, error_rate_threshold=0.5, consecutive_failures=4, open_seconds=20),
            overrides={f"llm:{c.model}": BreakerConfig(window=10, min_calls=3, error_rate_threshold=0.5,
                                                       consecutive_failures=3, open_seconds=30) for c in llms})
        self.gateway = Gateway(self.breakers, faults=faults, idem=self.store)
        self.chain = LLMChain(llms, self.gateway, timeout_s=settings.llm_timeout_s)
        self.ctx = context or ContextManager()
        self.tickets = TicketService(self.store._conn, self.store._lock)
        self.nodes = {
            "intake": self.n_intake, "route": self.n_route, "clarify": self.n_clarify,
            "recommend": self.n_recommend, "shipping_qa": self.n_shipping_qa, "static": self.n_static,
            "policy_qa": self.n_policy_qa, "protection": self.n_protection, "handoff": self.n_handoff,
            "respond": self.n_respond,
        }
        self.tool_spec = lambda name, model=None: CallSpec(
            dependency=f"tool:{name}", timeout_s=settings.tool_timeout_s, idempotent=True,
            retry=RetryPolicy(max_attempts=3, base_delay_s=0.05, max_delay_s=0.4), input_model=model)

    # =====================================================================
    # Turn lifecycle
    # =====================================================================
    def handle(self, user_message: str, session_id: str | None = None, request_id: str | None = None) -> dict:
        request_id = request_id or "r_" + uuid.uuid4().hex[:12]
        state = self.store.load(session_id) if session_id else None
        if session_id and state is None:
            state = SessionState(session_id=session_id)
        state = state or SessionState.new()
        if (cached := self.store.find_by_request(state.session_id, request_id)) is not None:
            cached["replayed"] = True
            return cached
        expected_version = state.version
        working = state.model_copy(deep=True)
        trace = Trace(working.session_id, working.turn + 1, request_id)
        trace.meta = {"prompt_version": prompts.PROMPT_VERSION, "llm_mode": self.s.llm_mode,
                      "models": [c.model for c in self.chain.clients]}
        turn = Turn(state=working, user_message=user_message, trace=trace, deadline=Deadline(self.s.turn_budget_s))

        node, steps = "intake", 0
        try:
            while node != "finalize":
                steps += 1
                if steps > self.s.max_steps:
                    trace.degrade("step_limit", f"exceeded {self.s.max_steps} steps")
                    turn.plan = {"type": "degraded"}
                    node = "respond_template"
                    break
                trace.path.append(node)
                with trace.span("node", node):
                    node = self.nodes[node](turn)
        except DeadlineExceeded as e:
            trace.degrade("deadline", str(e))
            turn.plan = {"type": "degraded", "message": "Sorry, that took longer than expected. Could you try again? "
                                                          "If it keeps happening I can connect you with support."}
            node = "respond_template"
        except Exception as e:  # noqa: BLE001 - last line of defense, never 500 the shopper
            trace.event("node", "unhandled_exception", status="error", error=f"{type(e).__name__}: {e}"[:300])
            trace.degrade("unhandled_exception", type(e).__name__)
            turn.plan = {"type": "degraded"}
            node = "respond_template"
        if node == "respond_template":
            turn.reply = template_respond(turn.plan)
            turn.cards, turn.chips = [], []
        response = self._finalize(turn)
        try:
            saved = self.store.save(turn.state, expected_version)
        except ConcurrentModification:
            trace.event("state", "concurrent_modification", status="error")
            raise
        self.store.record_turn(saved, request_id, user_message, response, trace.to_dict())
        try:
            write_jsonl(trace, Path(self.s.trace_jsonl))
        except OSError:
            pass
        return response

    def _finalize(self, t: Turn) -> dict:
        st = t.state
        st.messages.append(Message(role="assistant", content=t.reply, turn=st.turn))
        st.last_node = t.trace.path[-1] if t.trace.path else None
        if self.ctx.needs_compaction(st):
            with t.trace.span("context", "compaction") as sp:
                sp.attrs.update(self.ctx.compact(st, lambda prev, msgs: self._summarize(t, prev, msgs)))
        out_msgs = [{"type": "text", "text": t.reply}]
        if t.cards:
            out_msgs.append({"type": "product_cards", "items": t.cards})
        out_msgs.extend(t.blocks)
        if t.sources and re.search(r"\[\d+\]", t.reply):
            # Renumber cited sources 1..n in order of first appearance; drop uncited ones.
            by_n = {src["n"]: src for src in t.sources}
            order = list(dict.fromkeys(int(n) for n in re.findall(r"\[(\d+)\]", t.reply) if int(n) in by_n))
            remap = {old: i for i, old in enumerate(order, 1)}
            t.reply = re.sub(r"\[(\d+)\]", lambda m: f"[{remap[int(m.group(1))]}]" if int(m.group(1)) in remap else "", t.reply)
            out_msgs[0]["text"] = t.reply
            st.messages[-1].content = t.reply
            out_msgs.append({"type": "sources", "items": [
                {**{k: by_n[o][k] for k in ("id", "title", "section", "snippet")}, "n": remap[o]} for o in order]})
        if t.chips:
            out_msgs.append({"type": "chips", "options": t.chips})
        return {
            "session_id": st.session_id, "turn": st.turn, "request_id": t.trace.request_id,
            "messages": out_msgs, "degraded": t.trace.degradations, "trace_id": t.trace.trace_id,
            "intent": t.route.intent if t.route else None, "slots": st.slots.filled(),
            "latency_ms": round(t.trace._now_ms(), 1),
        }

    def _summarize(self, t: Turn, prev: str, msgs: list[Message]) -> str:
        text = "\n".join(f"{m.role}: {m.content}" for m in msgs)
        try:
            resp, out = self.chain.call(
                task="summarize", system=prompts.SUMMARIZER_SYSTEM, trace=t.trace, deadline=t.deadline, max_tokens=1500,
                messages=[{"role": "user", "content": f"Previous summary:\n{prev or '(none)'}\n\nNew conversation part:\n{text}"}],
                payload={"prev": prev, "messages": msgs})
            return out
        except (AllModelsFailed, HarnessError):
            t.trace.degrade("summary_extractive", "LLM summarizer unavailable")
            return extractive_summary(prev, msgs)

    # =====================================================================
    # Nodes
    # =====================================================================
    def n_intake(self, t: Turn) -> str:
        text, flags = check_input(t.user_message)
        t.flags = flags
        t.user_message = text
        t.state.turn += 1
        t.state.messages.append(Message(role="user", content=text, turn=t.state.turn))
        if flags:
            t.trace.event("guard", "input", status="blocked" if "injection_suspected" in flags else "ok", flags=flags)
        return "route"

    def n_route(self, t: Turn) -> str:
        st = t.state
        facts = [Fact("shown_item", f"#{i}", {"position": i, "title": kb.catalog_by_sku()[s]["title"]}, 2.0)
                 for i, s in enumerate(st.candidates, 1) if s in kb.catalog_by_sku()]
        pack = self.ctx.build(st, prompts.ROUTER_SYSTEM, "TASK: classify the latest message and extract slots.", facts)
        t.trace.event("context", "route_context", **pack.stats)
        try:
            _, decision = self.chain.call(
                task="route", system=pack.system, messages=pack.messages, json_schema=ROUTE_JSON_SCHEMA,
                trace=t.trace, deadline=t.deadline, max_tokens=3000,
                payload={"user_message": t.user_message, "state": st},
                parse=lambda txt: RouteDecision.model_validate_json(txt))
        except AllModelsFailed as e:
            t.trace.degrade("rules_router", str(e)[:200])
            decision = rule_route(t.user_message, st)
        t.route = decision
        if "injection_suspected" in t.flags or decision.injection_suspected:
            t.plan = {"type": "injection"}
            return "respond"
        if decision.referenced_index is None and st.candidates:
            low = t.user_message.lower()
            for i, sku in enumerate(st.candidates, 1):
                title = kb.catalog_by_sku().get(sku, {}).get("title", "")
                if title and title.lower() in low:
                    decision.referenced_index = i
                    break
        prev_clarify, st.last_clarify = st.last_clarify, None
        prev_intent = st.last_intent
        pending, st.pending = st.pending, None
        if pending and prev_clarify and self._answers_clarify(prev_clarify, decision, t.user_message):
            # The shopper answered our clarifying question: resume the parked question instead of starting over.
            t.trace.event("node", "resume_pending", pending=pending, answered=prev_clarify)
            decision.intent = pending["intent"]
            decision.question = pending["question"]
            if prev_clarify == "merchant" and not decision.merchant:
                decision.merchant = self._merchant_from_text(t.user_message)
        changed = self._merge_slots(t, decision)
        t.trace.event("state", "slots", changed=changed, slots=st.slots.filled())
        if (decision.intent in ("out_of_scope", "smalltalk") and prev_intent in ("protection_question", "shipping_question", "policy_question")
                and (decision.referenced_index is not None or re.match(r"\s*(what|how) about\b", t.user_message, re.I))):
            # "what about the cheapest one?" continues the previous topic with a new referent
            t.trace.event("node", "topic_carryover", intent=prev_intent)
            decision.intent = prev_intent
        if prev_clarify and decision.intent in ("out_of_scope", "smalltalk"):
            # "whatever is fine" / "no idea" right after a clarifying question = keep going, don't drop the flow
            t.trace.event("node", "clarify_followthrough", from_intent=decision.intent)
            decision.intent = "recommend"
        st.last_intent = decision.intent
        if decision.intent == "recommend":
            return self._needs_clarify(t, prev_clarify) or "recommend"
        if decision.intent == "shipping_question":
            return "shipping_qa"
        if decision.intent == "policy_question":
            return "policy_qa"
        if decision.intent == "protection_question":
            return "protection"
        if decision.intent == "order_issue":
            return "handoff"
        st.clarify_streak = 0
        return "static"

    @staticmethod
    def _merchant_from_text(text: str) -> str | None:
        low = text.lower()
        for mid, m in kb.merchants().items():
            if mid in low or m["name"].lower() in low:
                return m["name"]
        return None

    def _answers_clarify(self, slot: str, d: RouteDecision, text: str) -> bool:
        if slot == "merchant":
            return bool(d.merchant or self._merchant_from_text(text))
        if slot == "order_id":
            return bool(extract_order_id(text) or re.search(r"\b(don'?t|do not|can'?t|cannot) (have|find|remember)|no idea|not sure", text, re.I))
        if slot == "item":
            return d.referenced_index is not None
        if slot == "country":
            return bool(d.slots.country) and d.intent not in ("recommend",) or (bool(d.slots.country) and len(text.split()) <= 4)
        return False

    def _merge_slots(self, t: Turn, d: RouteDecision) -> list[str]:
        st, u, changed = t.state, d.slots, []

        def setv(name, value):
            if value is not None and getattr(st.slots, name) != value:
                setattr(st.slots, name, value)
                st.slot_turns[name] = st.turn
                changed.append(name)

        if u.country:
            c = kb.normalize_country(u.country)
            if c:
                setv("country", c)
            else:
                t.trace.event("state", "unknown_country", status="error", value=u.country)
        setv("budget_max", u.budget_max)
        if u.budget_max is not None and u.budget_min is None and st.slots.budget_min and st.slots.budget_min > u.budget_max:
            setv("budget_min", None)
            st.slots.budget_min = None
        setv("budget_min", u.budget_min)
        setv("recipient", u.recipient)
        if u.category and u.category != st.slots.category:
            st.slots.keywords = []           # shopper switched product type: old keywords are stale
            setv("category", u.category)
        if u.keywords:
            kws = [k.lower() for k in u.keywords if k.lower() != "gift"]
            if kws and kws != st.slots.keywords:
                st.slots.keywords = kws
                st.slot_turns["keywords"] = st.turn
                changed.append("keywords")
        prices = [kb.catalog_by_sku()[s]["price_usd"] for s in st.candidates if s in kb.catalog_by_sku()]
        if "cheaper" in d.modifiers and prices:
            setv("budget_max", round(min(prices) - 0.01, 2))
            st.slots.budget_min = None
        if "more_premium" in d.modifiers and prices:
            setv("budget_min", round(max(prices), 2))
            if st.slots.budget_max and st.slots.budget_max < max(prices) * 1.5:
                st.slots.budget_max = None
        return changed

    def _needs_clarify(self, t: Turn, prev_clarify: str | None = None) -> str | None:
        st = t.state
        s = st.slots
        if st.clarify_streak >= self.s.max_clarify_streak:
            t.trace.event("node", "clarify_cap_reached", streak=st.clarify_streak)
            return None
        if not (s.category or s.keywords or s.recipient):
            t.plan = {"type": "clarify", "missing": "what",
                      "question": "Happy to help! What kind of thing are you looking for, and who is it for?"}
            t.chips = ["Headphones", "A cozy sweater", "Home gift", "Skincare", "Outdoor gear"]
            return "clarify"
        if not s.country and prev_clarify != "country":   # never ask the same thing twice in a row
            t.plan = {"type": "clarify", "missing": "country",
                      "question": "Which country should it ship to? I'll check delivery time and import taxes for you."}
            t.chips = ["United States", "United Kingdom", "Germany", "Canada", "Australia", "Japan"]
            return "clarify"
        return None

    def n_clarify(self, t: Turn) -> str:
        t.state.clarify_streak += 1
        t.state.last_clarify = t.plan.get("missing") if t.plan else None
        return "respond"

    def n_static(self, t: Turn) -> str:
        intent = t.route.intent if t.route else "out_of_scope"
        if intent in ("smalltalk", "out_of_scope"):
            t.plan = {"type": intent}
        else:
            msg = {
            }.get(intent)
            t.plan = {"type": "unsupported_yet", "message": msg}
        return "respond"

    def n_recommend(self, t: Turn) -> str:
        st = t.state
        st.clarify_streak = 0
        s = st.slots
        args = dict(budget_max=s.budget_max, budget_min=s.budget_min, country=s.country, category=s.category,
                    keywords=s.keywords, limit=3)
        try:
            res = self.gateway.call(self.tool_spec("catalog", commerce.SearchInput), commerce.search_products, args,
                                    trace=t.trace, deadline=t.deadline)
        except HarnessError as e:
            t.trace.degrade("catalog_unavailable", e.kind)
            t.plan = {"type": "degraded", "message": "Sorry — our product catalog isn't responding right now. "
                                                     "Please try again in a minute."}
            return "respond"
        if not res["results"]:
            t.plan = {"type": "no_results", "filters": res["applied_filters"], "excluded": res["excluded"],
                      "restricted_examples": res["restricted_examples"], "slots": s.filled()}
            st.candidates = []
            return "respond"
        products = []
        shipping_down = False
        for p in res["results"]:
            ship = None
            if s.country and not shipping_down:
                try:
                    ship = self.gateway.call(self.tool_spec("shipping", commerce.SkuCountryInput),
                                             lambda sku, country: commerce.estimate_shipping(sku, country, self.s.sim_today),
                                             {"sku": p["sku"], "country": s.country}, trace=t.trace, deadline=t.deadline)
                except CircuitOpen:
                    shipping_down = True
                except HarnessError:
                    pass
                if ship is None:
                    t.trace.degrade("shipping_unavailable", f"no estimate for {p['sku']}")
            products.append({**p, "shipping": ship})
            t.facts.append(Fact("product", p["sku"], {k: p[k] for k in ("title", "merchant", "price_usd", "rating", "final_sale")}, 3.0))
            if ship:
                t.facts.append(Fact("shipping", p["sku"], {k: ship.get(k) for k in (
                    "country_name", "standard_days", "tax_usd", "duty_usd", "tax_name", "taxes_collected",
                    "shipping_fee_usd", "landed_total_usd", "delay_risk")}, 2.5))
        st.candidates = [p["sku"] for p in products]
        missing_shipping = bool(s.country) and any(p["shipping"] is None for p in products)
        t.plan = {"type": "recommend", "slots": s.filled(), "products": products,
                  "shipping_unavailable": missing_shipping, "total_matches": res["total_matches"]}
        t.cards = [self._card(p) for p in products]
        for i, p in enumerate(products, 1):
            sh = p.get("shipping") or {}
            if p["final_sale"] or p["price_usd"] >= 200 or sh.get("delay_risk") == "high":
                t.chips = [f"Is protection worth it for #{i}?"]
                t.trace.event("node", "protection_nudge", sku=p["sku"])
                break
        return "respond"

    def n_shipping_qa(self, t: Turn) -> str:
        st, s, d = t.state, t.state.slots, t.route
        st.clarify_streak = 0
        targets = list(st.candidates)
        if d and d.referenced_index and st.candidates:
            i = d.referenced_index
            idx = len(st.candidates) - 1 if i == -1 else i - 1
            targets = [st.candidates[idx]] if 0 <= idx < len(st.candidates) else []
        if not s.country:
            t.plan = {"type": "clarify", "missing": "country", "question": "Sure — which country is it shipping to?"}
            t.chips = ["United States", "United Kingdom", "Germany", "Canada", "Australia", "Japan"]
            st.clarify_streak += 1
            st.last_clarify = "country"
            st.pending = {"intent": "shipping_question", "question": d.question if d and d.question else t.user_message}
            return "respond"
        country = s.country
        rules = kb.shipping_rules()
        if country in rules["unsupported_destinations"]:
            t.plan = {"type": "shipping_answer", "country": country, "unsupported": rules["unsupported_destinations"][country], "items": []}
            t.facts.append(Fact("restriction", f"DEST-{country}", {"reason": rules["unsupported_destinations"][country]}, 3))
            return "respond"
        items = []
        for sku in targets[:3]:
            try:
                ship = self.gateway.call(self.tool_spec("shipping", commerce.SkuCountryInput),
                                         lambda sku, country: commerce.estimate_shipping(sku, country, self.s.sim_today),
                                         {"sku": sku, "country": country}, trace=t.trace, deadline=t.deadline)
            except HarnessError:
                t.trace.degrade("shipping_unavailable", sku)
                ship = None
            p = commerce.get_product(sku)
            items.append({"sku": sku, "title": p["title"], "price_usd": p["price_usd"], "shipping": ship})
            if ship:
                t.facts.append(Fact("shipping", sku, {"title": p["title"], **{k: ship.get(k) for k in (
                    "shippable", "reason", "standard_days", "express_days", "tax_usd", "duty_usd", "tax_name",
                    "taxes_collected", "landed_total_usd", "delay_risk")}}, 3))
        c = rules["countries"].get(country, {})
        days = commerce.standard_window(country, self.s.sim_today)
        general = {"country_name": c.get("name"), "standard_days": days, "tax_name": c.get("tax_name"),
                   "tax_rate_pct": round(c.get("tax_rate", 0) * 100), "duty_free_threshold_usd": c.get("duty_free_threshold_usd"),
                   "ddp": c.get("ddp_available")}
        t.facts.append(Fact("shipping_rules", country, general, 2))
        q = d.question if d and d.question else t.user_message
        if re.search(r"ddp|customs|gift|address|track|held|seiz|restrict|peak|christmas|holiday", q, re.I):
            hits = self._retrieve(t, q, {"doc_type": ["faq", "shipping", "restriction"]}, k=2)
            if hits:
                self._add_sources(t, hits, priority=2.6)
        t.plan = {"type": "shipping_answer", "country": country, "items": items, "general": general,
                  "question": q, "sources": [{"n": x["n"], "id": x["id"], "title": x["title"], "section": x["section"]} for x in t.sources],
                  "_source_texts": {x["n"]: x["text"] for x in t.sources}}
        return "respond"

    # ---------------- retrieval ----------------
    def _retrieve(self, t: Turn, query: str, filters: dict | None, k: int = 4) -> list | None:
        """Returns hits, [] when nothing relevant, or None when retrieval is unavailable."""
        spec = CallSpec(dependency="retrieval:kb", timeout_s=1.5, idempotent=True,
                        retry=RetryPolicy(max_attempts=2, base_delay_s=0.05, max_delay_s=0.2))
        try:
            # Scoped to one merchant's ~5 chunks, a weak lexical match is still meaningful: lower the floor.
            floor = 0.3 if filters and "merchant_id" in filters else 1.0
            res = self.gateway.call(spec, lambda query, filters, k: self.retriever.search(query, k=k, filters=filters, min_bm25=floor),
                                    {"query": query, "filters": filters, "k": k}, trace=t.trace, deadline=t.deadline)
        except HarnessError as e:
            t.trace.degrade("retrieval_unavailable", e.kind)
            return None
        t.trace.event("retrieval", "hits", query=query[:200], filters=filters,
                      hits=[{"id": h.chunk.id, "bm25": h.bm25, "char": h.char} for h in res["hits"]])
        return res["hits"]

    def _add_sources(self, t: Turn, hits: list, priority: float = 4.0) -> None:
        for h in hits:
            if any(src["id"] == h.chunk.id for src in t.sources):
                continue
            n = len(t.sources) + 1
            c = h.chunk
            text = c.text.replace("**", "")
            t.sources.append({"n": n, "id": c.id, "title": c.doc_title, "section": c.section, "text": text,
                              "snippet": text[:220] + ("…" if len(text) > 220 else "")})
            t.facts.append(Fact("source", str(n), {"title": c.doc_title, "section": c.section, "text": text},
                                priority - 0.1 * n))

    def _scope_item(self, t: Turn) -> dict | None:
        st, d = t.state, t.route
        if d and d.referenced_index and st.candidates:
            i = d.referenced_index
            idx = len(st.candidates) - 1 if i == -1 else i - 1
            if 0 <= idx < len(st.candidates):
                return kb.catalog_by_sku()[st.candidates[idx]]
        return None

    def n_policy_qa(self, t: Turn) -> str:
        st, d = t.state, t.route
        st.clarify_streak = 0
        question = (d.question if d and d.question else t.user_message)
        item = self._scope_item(t)
        merchant_id = None
        if d and d.merchant:
            merchant_id = next((mid for mid, m in kb.merchants().items()
                                if d.merchant.lower() in (mid, m["name"].lower()) or m["name"].lower().startswith(d.merchant.lower())), None)
        if not merchant_id and item:
            merchant_id = item["merchant_id"]
        if not merchant_id and st.candidates:
            mids = {kb.catalog_by_sku()[s]["merchant_id"] for s in st.candidates if s in kb.catalog_by_sku()}
            if len(mids) == 1:
                merchant_id = mids.pop()
        query = question + (f" {item['subcategory']} {item['title']}" if item else "")

        if d and d.intent == "protection_question":
            hits = self._retrieve(t, query, {"doc_type": ["protection", "faq"]}, k=3)
        else:
            if not merchant_id:
                names = [m["name"] for m in kb.merchants().values()]
                if st.candidates:
                    names = list(dict.fromkeys(kb.merchants()[kb.catalog_by_sku()[s]["merchant_id"]]["name"]
                                               for s in st.candidates if s in kb.catalog_by_sku()))
                t.plan = {"type": "clarify", "missing": "merchant",
                          "question": "Return rules differ by store. Which store is it from?"}
                t.chips = names
                st.clarify_streak += 1
                st.last_clarify = "merchant"
                st.pending = {"intent": d.intent if d else "policy_question", "question": question}
                return "respond"
            hits = self._retrieve(t, query, {"merchant_id": merchant_id}, k=3)
            if hits is not None:
                # Seel-style hook: where the store says no, Return Assurance may be the answer (if bought at checkout).
                extra = self._retrieve(t, query, {"doc_type": ["protection"]}, k=1)
                if extra and hits and any(re.search(r"cannot|can't|not be returned|non-returnable|final sale", h.chunk.text, re.I)
                                          for h in hits[:2]):
                    hits = hits + extra
        if hits is None:
            t.plan = {"type": "degraded", "message": "I can't look up the policy details right now, and I don't want to "
                                                     "guess. Please try again in a minute, or I can connect you with support."}
            return "respond"
        if not hits:
            store = kb.merchants()[merchant_id]["name"] if merchant_id else "our"
            t.plan = {"type": "no_answer", "message": f"I couldn't find anything about that in {store} policies. "
                                                      "I can connect you with our support team if you'd like."}
            return "respond"
        self._add_sources(t, hits)
        t.plan = {"type": "policy_answer", "question": question,
                  "merchant": kb.merchants()[merchant_id]["name"] if merchant_id else None,
                  "item": item["title"] if item else None,
                  "sources": [{"n": src["n"], "id": src["id"], "title": src["title"], "section": src["section"]} for src in t.sources],
                  "_source_texts": {src["n"]: src["text"] for src in t.sources}}
        return "respond"

    # ---------------- protection quote ----------------
    def n_protection(self, t: Turn) -> str:
        st, d = t.state, t.route
        st.clarify_streak = 0
        item = self._scope_item(t)
        if item is None and len(st.candidates) == 1:
            item = kb.catalog_by_sku()[st.candidates[0]]
        wants_price = bool(re.search(r"how much|cost|price|worth|should i|recommend|need it|do i need", t.user_message, re.I))
        if item is None:
            if st.candidates and wants_price:
                t.plan = {"type": "clarify", "missing": "item", "question": "Happy to price it. Which item is it for?"}
                t.chips = [f"#{i} {kb.catalog_by_sku()[s]['title']}" for i, s in enumerate(st.candidates, 1)]
                st.last_clarify, st.pending = "item", {"intent": "protection_question", "question": t.user_message}
                st.clarify_streak += 1
                return "respond"
            return "policy_qa"   # general coverage question: answer from the terms
        if not st.slots.country:
            t.plan = {"type": "clarify", "missing": "country",
                      "question": "Which country is it shipping to? The price depends on the delivery route."}
            t.chips = ["United States", "United Kingdom", "Germany", "Canada", "Australia", "Japan"]
            st.last_clarify, st.pending = "country", {"intent": "protection_question", "question": t.user_message}
            st.clarify_streak += 1
            st.candidates = st.candidates or [item["sku"]]
            return "respond"
        try:
            q = self.gateway.call(self.tool_spec("protection", QuoteInput),
                                  lambda sku, country: quote_protection(sku, country, self.s.sim_today),
                                  {"sku": item["sku"], "country": st.slots.country}, trace=t.trace, deadline=t.deadline)
        except HarnessError as e:
            t.trace.degrade("protection_quote_unavailable", e.kind)
            return "policy_qa"   # still explain coverage from the terms, just without a price
        if not q.get("available"):
            t.plan = {"type": "degraded", "message": f"Protection isn't available for this order: {q.get('reason')}"}
            return "respond"
        st.quotes[item["sku"]] = q
        hits = self._retrieve(t, "what is covered and not covered return assurance delivery protection",
                              {"doc_type": ["protection"]}, k=3)
        if hits:
            self._add_sources(t, hits, priority=2.5)
        t.facts.append(Fact("quote", item["sku"], {"title": q["title"], "item_price_usd": q["item_price_usd"],
                                                    "options": [{k: o[k] for k in ("name", "price_usd", "recommended", "reasons", "covers")}
                                                                for o in q["options"]],
                                                    "low_value_note": q["low_value_note"], "must_add_at_checkout": True}, 3.5))
        t.plan = {"type": "protection_quote", "quote": q,
                  "sources": [{"n": x["n"], "id": x["id"], "title": x["title"], "section": x["section"]} for x in t.sources],
                  "_source_texts": {x["n"]: x["text"] for x in t.sources}}
        t.blocks.append({"type": "protection_quote", "quote": {
            "sku": q["sku"], "title": q["title"], "item_price_usd": q["item_price_usd"], "country": q["country"],
            "low_value_note": q["low_value_note"],
            "options": [{k: o[k] for k in ("type", "name", "price_usd", "rate_pct", "recommended", "reasons", "covers")}
                        for o in q["options"]]}})
        return "respond"

    # ---------------- hand-off to support ----------------
    def n_handoff(self, t: Turn) -> str:
        st, d = t.state, t.route
        st.clarify_streak = 0
        user_texts = [m.content for m in st.messages if m.role == "user"][-4:]
        issue_text = " ".join(filter(None, [d.question if d else None, t.user_message]))
        issue = classify_issue(issue_text)
        if issue == "other":
            issue = classify_issue(" ".join(user_texts))
        order_id = next((o for o in (extract_order_id(x) for x in reversed(user_texts)) if o), None)
        no_order = re.search(r"\b(don'?t|do not|can'?t|cannot) (have|find|remember)|no idea|not sure", t.user_message, re.I)
        if not order_id and not no_order and "order_id" not in st.slot_turns:
            t.plan = {"type": "clarify", "missing": "order_id",
                      "question": "I'm sorry about that. What's your order number? It's in your confirmation email "
                                  "(if you can't find it, just say so and I'll still pass this on)."}
            st.last_clarify, st.pending = "order_id", {"intent": "order_issue", "question": issue_text}
            st.slot_turns["order_id"] = st.turn
            return "respond"
        summary = (f"Issue: {issue}. Order: {order_id or 'not provided'}. Destination: {st.slots.country or 'unknown'}. "
                   f"Shopper said: " + " | ".join(x[:200] for x in user_texts))
        args = {"session_id": st.session_id, "issue_type": issue, "summary": summary, "order_id": order_id,
                "country": st.slots.country, "skus": st.candidates[:3], "request_id": t.trace.request_id}
        spec = CallSpec(dependency="tool:tickets", timeout_s=self.s.tool_timeout_s, idempotent=False,
                        retry=RetryPolicy(max_attempts=3, base_delay_s=0.05, max_delay_s=0.4), input_model=TicketInput)
        try:
            ticket = self.gateway.call(spec, self.tickets.create_ticket, args, trace=t.trace, deadline=t.deadline,
                                       # request-scoped: protects retries of THIS turn; repeated complaints are
                                       # deduped by the ticket service itself (one open ticket per issue/order)
                                       idempotency_key=f"{st.session_id}:{t.trace.request_id}")
        except HarnessError as e:
            t.trace.degrade("ticket_unavailable", e.kind)
            t.plan = {"type": "degraded", "message": "I'm sorry - I couldn't open a support ticket just now. Please try "
                                                     "again in a minute, or use Contact Support in your order email."}
            return "respond"
        if not ticket.get("deduplicated"):
            st.tickets.append(ticket)
        st.status = "handoff"
        if issue in ("lost", "damaged"):
            hits = self._retrieve(t, f"parcel {issue} claim delivery protection", {"doc_type": ["protection"]}, k=2)
            if hits:
                self._add_sources(t, hits, priority=2.5)
        t.facts.append(Fact("ticket", ticket["ticket_id"], ticket, 3.5))
        t.plan = {"type": "handoff", "ticket": ticket, "issue": issue,
                  "sources": [{"n": x["n"], "id": x["id"], "title": x["title"], "section": x["section"]} for x in t.sources],
                  "_source_texts": {x["n"]: x["text"] for x in t.sources}}
        t.blocks.append({"type": "ticket", "ticket": ticket})
        return "respond"

    def n_respond(self, t: Turn) -> str:
        plan = t.plan or {"type": "out_of_scope"}
        if plan["type"] in DETERMINISTIC_PLANS:
            t.reply = template_respond(plan)
            t.trace.event("respond", "template", plan=plan["type"])
            return "finalize"
        allowed = numbers_in({k: v for k, v in plan.items() if k != "_source_texts"}) + numbers_in(t.state.slots.model_dump())
        for txt in (plan.get("_source_texts") or {}).values():
            allowed += [float(x) for x in re.findall(r"\$(\d+(?:\.\d{1,2})?)", txt)]
        for p in plan.get("products", []) + plan.get("items", []):
            sh = p.get("shipping") or {}
            allowed.append((sh.get("tax_usd") or 0) + (sh.get("duty_usd") or 0))
        task_block = "RESPONSE PLAN:\n" + json.dumps(_plan_for_llm(plan), ensure_ascii=False)
        pack = self.ctx.build(t.state, prompts.RESPONDER_SYSTEM, task_block, t.facts)
        t.trace.event("context", "respond_context", **pack.stats)
        messages = pack.messages
        for attempt in range(2):
            try:
                _, text = self.chain.call(task="respond", system=pack.system, messages=messages, trace=t.trace,
                                          deadline=t.deadline, max_tokens=3000, payload={"plan": plan})
            except AllModelsFailed as e:
                t.trace.degrade("template_reply", str(e)[:200])
                t.reply = template_respond(plan)
                return "finalize"
            text = normalize_citations(text)
            g = check_output(text, allowed)
            if plan.get("_source_texts") and plan["type"] == "policy_answer":
                gg = check_grounding(text, plan["_source_texts"])
                g.violations += gg.violations
                g.ok = g.ok and gg.ok
            t.trace.event("guard", "output", status="ok" if g.ok else "blocked", attempt=attempt, violations=g.violations)
            if g.ok:
                t.reply = text.strip()
                return "finalize"
            messages = messages + [{"role": "assistant", "content": text},
                                   {"role": "user", "content": "That draft broke the rules: " + json.dumps(g.violations)
                                    + ". Rewrite it using only numbers from FACTS and without promises."}]
        t.trace.degrade("guard_fallback_template", "draft failed output guard twice")
        t.reply = template_respond(plan)
        return "finalize"

    @staticmethod
    def _card(p: dict) -> dict:
        sh = p.get("shipping")
        card = {"sku": p["sku"], "title": p["title"], "merchant": p["merchant"], "price_usd": p["price_usd"],
                "rating": p["rating"], "reviews": p["reviews"], "final_sale": p["final_sale"],
                "category": p["category"], "subcategory": p["subcategory"], "shipping": None}
        if sh and sh.get("shippable"):
            card["shipping"] = {"days": sh["standard_days"], "taxes_usd": round(sh["tax_usd"] + sh["duty_usd"], 2),
                                "tax_name": sh["tax_name"], "collected": sh["taxes_collected"],
                                "fee_usd": sh["shipping_fee_usd"], "landed_total_usd": sh["landed_total_usd"],
                                "delay_risk": sh["delay_risk"], "country": sh["country"]}
        return card


def _plan_for_llm(plan: dict) -> dict:
    """The LLM gets the plan's intent and constraints; detailed numbers are in FACTS."""
    p = {k: v for k, v in plan.items() if k not in ("products", "items") and not k.startswith("_")}
    if "products" in plan:
        p["products"] = [{"title": x["title"], "position": i} for i, x in enumerate(plan["products"], 1)]
    if "items" in plan:
        p["items"] = [{"title": x["title"], "position": i} for i, x in enumerate(plan["items"], 1)]
    p["instructions"] = {
        "recommend": "Introduce the picks in 1 short sentence each (why it fits). Mention delivery window and estimated landed total from FACTS. If shipping_unavailable is true, say delivery/tax info is temporarily unavailable. End with one helpful question.",
        "no_results": "Explain briefly why nothing matched (using excluded counts / restrictions) and suggest one concrete change.",
        "shipping_answer": "Answer the shipping/tax question for the destination using FACTS. If an item cannot ship, say why and suggest an alternative approach. If numbered sources are provided and you use them, cite them like [1].",
        "protection_quote": "Give an honest recommendation for each option in 1-2 sentences using the quote's 'recommended' flag and 'reasons' (don't push protection that isn't recommended). Mention prices from FACTS, that it can only be added at checkout, and cite what's covered or excluded with [n] if sources are given.",
        "handoff": "Apologize briefly, confirm the support ticket id and that a support specialist will follow up. Do NOT promise a refund, replacement, or outcome. If sources about Delivery Protection are given, say that if it was added at checkout a claim can be reviewed, citing [n].",
        "policy_answer": "Answer the question directly in 2-4 sentences using only the numbered sources, citing each statement like [1]. If the sources don't cover it, say so.",
    }.get(plan["type"], "Answer helpfully and briefly.")
    return p
