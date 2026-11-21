from app.agent.graph import Agent
from app.config import Settings
from app.harness.faults import Fault, FaultInjector
from app.harness.state import SessionStore
from app.llm.base import LLMResponse
from app.llm.mock import MockLLM
from app.tools.protection import quote_protection
from app.tools.tickets import classify_issue, extract_order_id


def make_agent(faults=None, llms=None):
    s = Settings(llm_mode="mock", db_path=":memory:", trace_jsonl="/dev/null", sim_today="2026-11-21")
    return Agent(s, llms or [MockLLM()], store=SessionStore(":memory:"), faults=faults)


def chat(agent, msgs):
    sid, out = None, []
    for m in msgs:
        r = agent.handle(m, sid)
        sid = r["session_id"]
        out.append(r)
    return out


def block(r, kind):
    return next((m for m in r["messages"] if m["type"] == kind), None)


def text(r):
    return r["messages"][0]["text"]


class ScriptedResponder:
    def __init__(self, reply):
        self.reply, self.model, self._mock = reply, "scripted", MockLLM()

    def complete(self, *, task, **kw):
        if task == "respond":
            return LLMResponse(text=self.reply, model=self.model)
        return self._mock.complete(task=task, **kw)


# ---------------- pricing model ----------------

def test_quote_is_bounded_and_explainable():
    q = quote_protection("MAP-046", "AU", "2026-11-21")   # $369.99 bag, high-risk lane, peak season
    dp = next(o for o in q["options"] if o["type"] == "delivery_protection")
    assert dp["recommended"] and "peak season" in dp["factors"]
    for o in q["options"]:
        assert 0.99 <= o["price_usd"] <= 0.06 * q["item_price_usd"] + 0.01


def test_final_sale_item_recommends_return_assurance_unless_cheap():
    from app.tools import kb
    tees = [p for p in kb.catalog() if p["final_sale"]]
    q = quote_protection(tees[0]["sku"], "DE", "2026-11-21")
    ra = next(o for o in q["options"] if o["type"] == "return_assurance")
    assert "final sale item" in ra["factors"]
    assert ra["recommended"] == (tees[0]["price_usd"] >= 30)


def test_quote_refuses_restricted_route():
    from app.tools import kb
    pb = next(p for p in kb.catalog() if p["battery"] == "standalone")
    assert quote_protection(pb["sku"], "AU", "2026-11-21")["available"] is False


# ---------------- protection flow ----------------

def test_recommend_nudges_protection_then_quotes_referenced_item():
    rs = chat(make_agent(), ["leather bag under $400 to Australia", "Is protection worth it for #1?", "what about the cheapest one?"])
    assert any("protection" in c.lower() for c in block(rs[0], "chips")["options"])
    q1, q2 = block(rs[1], "protection_quote")["quote"], block(rs[2], "protection_quote")["quote"]
    assert q1["sku"] != q2["sku"] and q2["item_price_usd"] < q1["item_price_usd"]
    assert "added at checkout" in text(rs[1])


def test_ambiguous_item_asks_which_then_quotes():
    rs = chat(make_agent(), ["organic cotton tee to Germany", "should I get protection for it?", "#2"])
    assert block(rs[1], "chips") and not block(rs[1], "protection_quote")
    assert block(rs[2], "protection_quote")


def test_general_coverage_question_answers_from_terms_without_quote():
    r = chat(make_agent(), ["what does delivery protection cover?"])[-1]
    assert not block(r, "protection_quote")
    assert block(r, "sources")


# ---------------- hand-off ----------------

def test_issue_and_order_parsing():
    assert classify_issue("my parcel never arrived") == "lost"
    assert classify_issue("the mug arrived cracked") == "damaged"
    assert extract_order_id("it's order #A88231") == "A88231"
    assert extract_order_id("I ordered 2 mugs") is None


def test_handoff_asks_order_once_then_opens_ticket_and_dedupes():
    agent = make_agent()
    rs = chat(agent, ["my parcel never arrived", "it's order #A88231", "hello?? still nothing arrived"])
    assert "order number" in text(rs[0])
    t1, t2 = block(rs[1], "ticket")["ticket"], block(rs[2], "ticket")["ticket"]
    assert t1["ticket_id"] == t2["ticket_id"] and t2["deduplicated"]
    assert len(agent.tickets.list()) == 1
    assert t1["priority"] == "high" and t1["order_id"] == "A88231"


def test_handoff_without_order_number_still_opens_ticket():
    rs = chat(make_agent(), ["my mug arrived broken", "I don't have the order number"])
    assert block(rs[1], "ticket")["ticket"]["issue_type"] == "damaged"


def test_lost_response_after_commit_does_not_duplicate_ticket():
    """Tool commits, then the response is lost (timeout) -> gateway retries -> still one ticket."""
    agent = make_agent()
    real = agent.tickets.create_ticket
    calls = {"n": 0}

    def flaky(**kw):
        calls["n"] += 1
        out = real(**kw)
        if calls["n"] == 1:
            raise ConnectionResetError("response lost after commit")
        return out

    agent.tickets.create_ticket = flaky
    r = chat(agent, ["my parcel never arrived, order #ZX12345"])[-1]
    assert calls["n"] == 2 and len(agent.tickets.list()) == 1
    assert block(r, "ticket")["ticket"]["deduplicated"] is False   # shopper complained once: "opened", not "already open"
    assert "I've opened support ticket" in text(r)


def test_ticket_outage_degrades_without_promises():
    agent = make_agent(FaultInjector({"tool:tickets": Fault(down=True)}))
    r = chat(agent, ["my parcel never arrived, order #ZX12345"])[-1]
    assert "ticket_unavailable" in r["degraded"] and not block(r, "ticket")
    assert "refund" not in text(r).lower()


def test_guard_blocks_refund_promise_in_handoff():
    liar = ScriptedResponder("So sorry! Don't worry, you'll get a full refund within 3 days.")
    r = chat(make_agent(llms=[liar]), ["my parcel never arrived, order #ZX12345"])[-1]
    assert "guard_fallback_template" in r["degraded"]
    assert "full refund" not in text(r) and block(r, "ticket")
