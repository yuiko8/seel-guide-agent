from app.agent.graph import Agent
from app.config import Settings
from app.harness.faults import Fault, FaultInjector
from app.harness.guardrails import check_grounding, normalize_citations
from app.harness.state import SessionStore
from app.llm.base import LLMResponse
from app.llm.mock import MockLLM
from app.rag.retriever import HybridRetriever


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


def text(r):
    return r["messages"][0]["text"]


def source_ids(r):
    return [i["id"] for m in r["messages"] if m["type"] == "sources" for i in m["items"]]


class ScriptedResponder:
    """Routes with the rule router, but 'writes' a fixed reply - to test the output guard."""

    def __init__(self, reply, model="scripted"):
        self.reply, self.model, self._mock = reply, model, MockLLM()

    def complete(self, *, task, **kw):
        if task == "respond":
            return LLMResponse(text=self.reply, model=self.model)
        return self._mock.complete(task=task, **kw)


# ---------------- retriever ----------------

def test_retriever_filters_by_merchant():
    r = HybridRetriever()
    hits = r.search("return window", k=5, filters={"merchant_id": "kiko"})["hits"]
    assert hits and all(h.chunk.meta["merchant_id"] == "kiko" for h in hits)


def test_retriever_confidence_floor_returns_nothing_for_nonsense():
    r = HybridRetriever()
    assert r.search("zxqv blorp wibble", k=4)["hits"] == []


# ---------------- grounding guard ----------------

def test_grounding_flags_uncited_and_invented_numbers():
    src = {1: "Returns accepted within 14 days of delivery."}
    g = check_grounding("You have 30 days to return it.", src)
    kinds = {v["type"] for v in g.violations}
    assert kinds == {"uncited_answer", "ungrounded_claim"}
    assert check_grounding("You have 14 days to return it [1].", src).ok
    assert not check_grounding("See [3].", src).ok
    assert normalize_citations("ok [source:2]") == "ok [2]"


# ---------------- policy QA flows ----------------

def test_policy_answer_is_scoped_to_referenced_items_merchant_and_cited():
    rs = chat(make_agent(), ["noise-cancelling headphones under $150 to Germany", "can I return the second one if I opened it?"])
    ids = source_ids(rs[-1])
    assert ids and all(i.startswith("policy:nordlys") or i.startswith("protection") for i in ids)
    assert "[1]" in text(rs[-1])


def test_final_sale_answer_surfaces_return_assurance_hook():
    r = chat(make_agent(), ["can I return a final sale tee from Maple?"])[-1]
    assert "policy:maple#final-sale" in source_ids(r)
    assert "protection#return-assurance" in source_ids(r)
    assert "added at checkout" in text(r)


def test_ambiguous_store_asks_then_resumes_parked_question():
    rs = chat(make_agent(), ["what's the return policy?", "Kiko Home"])
    assert "Which store" in text(rs[0])
    assert rs[1]["intent"] == "policy_question"
    assert "policy:kiko#return-window" in source_ids(rs[1])


def test_retrieval_outage_never_guesses_policy():
    agent = make_agent(FaultInjector({"retrieval:kb": Fault(down=True)}))
    r = chat(agent, ["can I return a final sale tee from Maple?"])[-1]
    assert "retrieval_unavailable" in r["degraded"]
    assert "don't want to guess" in text(r)
    assert not source_ids(r)


def test_guard_blocks_hallucinated_policy_and_falls_back_to_sources():
    liar = ScriptedResponder("Sure! Maple accepts final sale returns within 60 days and you will get a full refund.")
    r = chat(make_agent(llms=[liar]), ["can I return a final sale tee from Maple?"])[-1]
    assert "guard_fallback_template" in r["degraded"]
    assert "60 days" not in text(r) and "[1]" in text(r)


def test_guard_accepts_grounded_cited_answer():
    honest = ScriptedResponder("Items marked Final Sale cannot be returned or exchanged [1].")
    r = chat(make_agent(llms=[honest]), ["can I return a final sale tee from Maple?"])[-1]
    assert r["degraded"] == []
    assert text(r).startswith("Items marked Final Sale")


def test_protection_question_uses_protection_docs():
    r = chat(make_agent(), ["what does return assurance cover?"])[-1]
    assert source_ids(r)[0].startswith("protection#")


def test_shipping_answer_explains_no_ddp():
    r = chat(make_agent(), ["what does DDP mean for Brazil?"])[-1]
    assert "courier collects duties" in text(r)
