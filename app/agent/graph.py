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
from ..harness.guardrails import check_input, check_output, numbers_in
from ..harness.retry import RetryPolicy
from ..harness.state import ConcurrentModification, Message, SessionState, SessionStore
from ..harness.tracing import Trace, write_jsonl
from ..llm.base import LLMClient
from ..tools import commerce, kb
from . import prompts
from .llm_chain import AllModelsFailed, LLMChain
from .rules import rule_route, template_respond
from .schemas import ROUTE_JSON_SCHEMA, RouteDecision

DETERMINISTIC_PLANS = {"clarify", "smalltalk", "out_of_scope", "injection", "unsupported_yet", "degraded"}


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
    reply: str = ""


class Agent:
    def __init__(self, settings: Settings, llms: list[LLMClient], store: SessionStore | None = None,
                 faults: FaultInjector | None = None, breakers: BreakerRegistry | None = None,
                 context: ContextManager | None = None):
        self.s = settings
        self.store = store or SessionStore(settings.db_path)
        self.breakers = breakers or BreakerRegistry(
            BreakerConfig(window=20, min_calls=5, error_rate_threshold=0.5, consecutive_failures=4, open_seconds=20),
            overrides={f"llm:{c.model}": BreakerConfig(window=10, min_calls=3, error_rate_threshold=0.5,
                                                       consecutive_failures=3, open_seconds=30) for c in llms})
        self.gateway = Gateway(self.breakers, faults=faults, idem=self.store)
        self.chain = LLMChain(llms, self.gateway, timeout_s=settings.llm_timeout_s)
        self.ctx = context or ContextManager()
        self.nodes = {
            "intake": self.n_intake, "route": self.n_route, "clarify": self.n_clarify,
            "recommend": self.n_recommend, "shipping_qa": self.n_shipping_qa, "static": self.n_static,
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
        prev_clarify, st.last_clarify = st.last_clarify, None
        changed = self._merge_slots(t, decision)
        t.trace.event("state", "slots", changed=changed, slots=st.slots.filled())
        if prev_clarify and decision.intent in ("out_of_scope", "smalltalk"):
            # "whatever is fine" / "no idea" right after a clarifying question = keep going, don't drop the flow
            t.trace.event("node", "clarify_followthrough", from_intent=decision.intent)
            decision.intent = "recommend"
        if decision.intent == "recommend":
            return self._needs_clarify(t, prev_clarify) or "recommend"
        if decision.intent == "shipping_question":
            return "shipping_qa"
        st.clarify_streak = 0
        return "static"

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
                "policy_question": "I can't look up return policies just yet — that's coming soon. Our support team can answer this for you right away.",
                "protection_question": "Purchase protection quotes are coming soon. In the meantime our support team can tell you what's covered.",
                "order_issue": "I'm sorry about the trouble with your order. I'm connecting you with our support team, who can look into it.",
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
        t.plan = {"type": "shipping_answer", "country": country, "items": items, "general": general,
                  "question": d.question if d else t.user_message}
        return "respond"

    def n_respond(self, t: Turn) -> str:
        plan = t.plan or {"type": "out_of_scope"}
        if plan["type"] in DETERMINISTIC_PLANS:
            t.reply = template_respond(plan)
            t.trace.event("respond", "template", plan=plan["type"])
            return "finalize"
        allowed = numbers_in(plan) + numbers_in(t.state.slots.model_dump())
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
            g = check_output(text, allowed)
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
    p = {k: v for k, v in plan.items() if k not in ("products", "items")}
    if "products" in plan:
        p["products"] = [{"title": x["title"], "position": i} for i, x in enumerate(plan["products"], 1)]
    if "items" in plan:
        p["items"] = [{"title": x["title"], "position": i} for i, x in enumerate(plan["items"], 1)]
    p["instructions"] = {
        "recommend": "Introduce the picks in 1 short sentence each (why it fits). Mention delivery window and estimated landed total from FACTS. If shipping_unavailable is true, say delivery/tax info is temporarily unavailable. End with one helpful question.",
        "no_results": "Explain briefly why nothing matched (using excluded counts / restrictions) and suggest one concrete change.",
        "shipping_answer": "Answer the shipping/tax question for the destination using FACTS. If an item cannot ship, say why and suggest an alternative approach.",
    }.get(plan["type"], "Answer helpfully and briefly.")
    return p
