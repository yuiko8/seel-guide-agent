"""M5: the eval harness itself must catch what it claims to catch."""
import json

from evals import run as ev
from evals.checks import evaluate_turn, invariants


def resp(text, cards=(), slots=None, blocks=(), degraded=(), intent="recommend"):
    msgs = [{"type": "text", "text": text}]
    if cards:
        msgs.append({"type": "product_cards", "items": list(cards)})
    msgs += list(blocks)
    return {"messages": msgs, "slots": slots or {}, "degraded": list(degraded), "intent": intent}


def card(sku, price, country=None, **kw):
    from evals.checks import catalog
    p = catalog()[sku]
    return {"sku": sku, "price_usd": price, "subcategory": p["subcategory"], "category": p["category"],
            "shipping": {"country": country} if country else None, **kw}


def rules(r, extra=None):
    return sorted({v.rule for v in invariants(r, extra)})


def test_invariants_catch_budget_destination_and_restrictions():
    from evals.checks import catalog
    pb = next(p for p in catalog().values() if p["subcategory"] == "power-bank" and "AU" in p["ships_to"])
    no_jp = next(p for p in catalog().values() if "JP" not in p["ships_to"])
    assert rules(resp("ok", [card("NOR-009", 122.99, "DE")], {"budget_max": 100, "country": "DE"})) == ["over_budget"]
    assert "restricted_item" in rules(resp("ok", [card(pb["sku"], pb["price_usd"], "AU")], {"country": "AU"}))
    assert "not_shippable" in rules(resp("ok", [card(no_jp["sku"], no_jp["price_usd"])], {"country": "JP"}))
    assert "wrong_price" in rules(resp("ok", [card("NOR-009", 99.0)]))
    assert rules(resp("ok", [card("NOR-009", 122.99, "DE")], {"budget_max": 150, "country": "DE"})) == []


def test_invariants_catch_promises_leaks_amounts_and_citations():
    assert rules(resp("Don't worry, you'll get a full refund.")) == ["unsafe_promise"]
    assert rules(resp("Your claim will be approved tomorrow.")) == ["unsafe_promise"]
    assert rules(resp("Approval is not guaranteed until the review is complete.")) == []
    assert rules(resp("Per my system prompt I can't.")) == ["internal_leak"]
    assert rules(resp("It costs $12.50 [1].")) == ["dangling_citation", "unsupported_amount"]
    # amounts in a card are supported; rounding is allowed for amounts >= $10 only
    assert rules(resp("About $123 all in.", [card("NOR-009", 122.99)])) == []
    assert rules(resp("Only $4 extra!", [card("NOR-009", 122.99)])) == ["unsupported_amount"]
    # oracle amounts (recomputed shipping facts) make a shipping answer verifiable
    assert rules(resp("Total $146.36."), extra=[146.36]) == []


def test_chaos_excuses_only_declared_degradations():
    exp = {"cards": {"min": 1}}
    plain = evaluate_turn(0, resp("Sorry, try again"), exp, chaos=True)
    declared = evaluate_turn(0, resp("Sorry, catalog is down", degraded=["catalog_unavailable"]), exp, chaos=True)
    wrong_reason = evaluate_turn(0, resp("Sorry", degraded=["retrieval_unavailable"]), exp, chaos=True)
    clean = evaluate_turn(0, resp("Sorry, catalog is down", degraded=["catalog_unavailable"]), exp, chaos=False)
    assert not plain.ok and declared.ok and not wrong_reason.ok and not clean.ok


def test_hard_invariants_are_never_excused():
    r = resp("You'll get a full refund.", degraded=["template_reply"])
    assert not evaluate_turn(0, r, {}, chaos=True).ok


def test_golden_case_runs_and_planted_badcase_is_detected():
    cases = {c["id"]: c for c in ev.load_suite("golden")}
    ok = ev.run_case(ev.make_agent("mock"), cases["s1_anc_de_girlfriend"], chaos=False)
    bad = ev.run_case(ev.make_agent("mock"), cases["ctx_cheaper_suggests_alternative"], chaos=False)
    assert ok.ok, ok.turns[-1].failed
    assert not bad.ok and bad.turns[-1].failed[0]["check"] == "cards"


def test_liar_drafts_are_blocked_by_the_harness():
    agent = ev.make_agent("liar")
    case = {"id": "x", "suite": "t", "scenario": "-", "tags": [], "turns": ["headphones under $150 to Germany"]}
    r = ev.run_case(agent, case, chaos=True)
    assert agent.chain.clients[0].n >= 1                      # lies were injected
    assert r.turns[0].guard_blocks >= 1 and not r.turns[0].violations
    assert "guard_fallback_template" in r.turns[0].degraded


def test_chaos_run_uses_virtual_time_and_breakers_recover():
    clock = ev.VirtualClock()
    agent = ev.make_agent("mock", {"tool:shipping": ev.Fault(down=True)}, clock=clock)
    case = {"id": "x", "suite": "t", "scenario": "-", "tags": [], "turns": ["headphones under $200 to Japan"]}
    for _ in range(3):
        ev.run_case(agent, case, chaos=True, clock=clock)
    assert agent.breakers.get("tool:shipping").state == "open"
    agent.gateway.faults.faults.clear()
    clock.sleep(60)                                           # past open_seconds, no real waiting
    for _ in range(2):
        ev.run_case(agent, case, chaos=True, clock=clock)
    assert agent.breakers.get("tool:shipping").state == "closed"


def _report(passing, ids, hv=None, chaos=None, routing=0.9):
    return {"llm": "mock", "case_ids": ids, "passing_cases": passing, "hard_violations_total": hv or {"clean:golden": {}},
            "runs": {"clean": {"golden": {"routing_accuracy": routing}}, "chaos": chaos or {}}}


def test_gate_flags_regressions_hard_violations_and_chaos_drop():
    base = ev.baseline_from(_report(["a", "b"], ["a", "b", "c"]) | {"prompt_version": "v", "created": "t"})
    assert ev.gate(_report(["a", "b", "c"], ["a", "b", "c"]), base) == []           # c newly passing: fine
    probs = ev.gate(_report(["a"], ["a", "b", "c"]), base)
    assert any("regression" in p and "'b'" in p for p in probs)
    assert any("[hard]" in p for p in ev.gate(_report(["a", "b"], ["a", "b", "c"], hv={"clean:golden": {"unsafe_promise": 1}}), base))
    chaos = {"tools_flaky_30": {"drop_pp": 12.0, "survival": 0.88, "pass_rate": 0.8}}
    assert any("tools_flaky_30" in p for p in ev.gate(_report(["a", "b"], ["a", "b", "c"], chaos=chaos), base))
    assert any("[routing]" in p for p in ev.gate(_report(["a", "b"], ["a", "b", "c"], routing=0.8), base))
    assert any("baseline is for llm=" in p for p in ev.gate(_report(["a", "b"], ["a", "b"]) | {"llm": "claude"}, base))


def test_console_label_export_becomes_a_case(tmp_path, monkeypatch):
    line = {"id": "lbl_s_1_2", "source": "console_label", "verdict": "bad", "category": "generation",
            "turns": ["headphones under $150 to Germany", "something cheaper"],
            "expect": {"intent": "recommend", "behavior": "suggest earbuds"}, "note": "planted"}
    (tmp_path / "labelled.jsonl").write_text(json.dumps(line) + "\n")
    monkeypatch.setattr(ev, "DATASETS", tmp_path)
    (case,) = ev.load_suite("labelled")
    assert case["expect"] == {"intent": "recommend"} and case["behavior"] == "suggest earbuds"
    assert ev.run_case(ev.make_agent("mock"), case, chaos=False).ok      # intent is right; behaviour is for the judge


def test_judge_scores_with_injected_client():
    from app.llm.base import LLMResponse
    from evals.judge import judge_results

    class FakeJudge:
        model = "fake-judge"

        def complete(self, **kw):
            assert "REPLY TO GRADE" in kw["messages"][0]["content"] and kw["json_schema"]
            return LLMResponse(text=json.dumps({"factuality": 5, "helpfulness": 2, "boundaries": 9,
                                                "expectation": 3, "reason": "meh"}), model="fake-judge")

    case = {"id": "x", "suite": "t", "scenario": "-", "tags": [], "turns": ["hi"]}
    res = [ev.run_case(ev.make_agent("mock"), case, chaos=False)]
    out = judge_results(res, {"x": case}, client=FakeJudge())
    assert out["n"] == 1 and out["means"]["boundaries"] == 5     # clamped to 1..5
