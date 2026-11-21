"""M4 ops console API: traces, stats, chaos/breaker admin, labels -> eval export."""
import json

import pytest
from fastapi.testclient import TestClient

from app.agent.graph import Agent
from app.api import create_app
from app.config import Settings
from app.harness.faults import FaultInjector
from app.harness.state import SessionStore
from app.harness.tracing import Trace, write_jsonl
from app.llm.mock import MockLLM


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_CHAOS", "1")
    s = Settings(llm_mode="mock", db_path=":memory:", trace_jsonl=str(tmp_path / "t.jsonl"), sim_today="2026-11-21")
    agent = Agent(s, [MockLLM()], store=SessionStore(":memory:"), faults=FaultInjector(seed=1))
    return TestClient(create_app(agent))


def chat(c, msg, sid=None):
    r = c.post("/api/chat", json={"message": msg, "session_id": sid})
    assert r.status_code == 200
    return r.json()


def test_trace_list_detail_and_llm_span_usage(client):
    a = chat(client, "noise cancelling headphones under $150 to Germany")
    chat(client, "something cheaper", a["session_id"])
    rows = client.get("/api/traces").json()
    assert len(rows) == 2 and rows[0]["turn"] == 2            # newest first
    assert rows[0]["user_message"] == "something cheaper"
    assert rows[0]["meta"]["prompt_version"]
    d = client.get(f"/api/traces/{a['trace_id']}").json()
    assert d["trace"]["path"][0] == "intake"
    llm = [sp for sp in d["trace"]["spans"] if sp["kind"] == "llm" and sp["name"].startswith("llm:")]
    assert llm and all("task" in sp["attrs"] and "input_tokens" in sp["attrs"] for sp in llm)
    assert client.get("/api/traces/nope").status_code == 404
    assert client.get("/api/traces", params={"session_id": a["session_id"], "q": "cheaper"}).json()[0]["turn"] == 2


def test_chaos_marks_degraded_and_filters(client):
    chat(client, "hi")
    r = client.post("/api/admin/faults/tool:catalog", json={"down": True})
    assert r.status_code == 200 and r.json()["faults"]["tool:catalog"]["down"] is True
    assert client.post("/api/admin/faults/tool:nonsense", json={"down": True}).status_code == 404
    out = chat(client, "noise cancelling headphones under $150 to Germany")
    assert "catalog_unavailable" in out["degraded"]
    h = client.get("/api/health").json()
    assert h["injected"]["tool:catalog"] >= 1 and "retrieval:kb" in h["dependencies"]
    deg = client.get("/api/traces", params={"only": "degraded"}).json()
    assert [t["trace_id"] for t in deg] == [out["trace_id"]]
    assert client.get("/api/traces", params={"only": "bogus"}).status_code == 400
    assert client.delete("/api/admin/faults").json()["faults"] == {}
    st = client.get("/api/stats").json()
    assert st["window"] == 2 and st["degraded_rate"] == 0.5 and st["degradations"]["catalog_unavailable"] == 1


def test_force_breaker_open_then_closed(client):
    r = client.post("/api/admin/breakers/tool:shipping/open")
    assert r.status_code == 200 and r.json()["state"] == "open"
    out = chat(client, "headphones under $150 to Germany")
    assert "shipping_unavailable" in out["degraded"]
    assert client.post("/api/admin/breakers/tool:shipping/closed").json()["state"] == "closed"
    assert client.post("/api/admin/breakers/tool:shipping/half").status_code == 400
    assert client.post("/api/admin/breakers/tool:made_up/open").status_code == 404


def test_labels_upsert_filter_and_export(client):
    a = chat(client, "noise cancelling headphones under $150 to Germany")
    b = chat(client, "something cheaper", a["session_id"])
    sid = a["session_id"]
    bad = {"session_id": sid, "turn": 2, "verdict": "bad", "category": "generation",
           "note": "says nothing found", "expected_behavior": "suggest earbuds"}
    assert client.post("/api/labels", json=bad).json()["category"] == "generation"
    # relabel same turn -> overwrite, not duplicate
    assert client.post("/api/labels", json={**bad, "category": "retrieval"}).json()["category"] == "retrieval"
    client.post("/api/labels", json={"session_id": sid, "turn": 1, "verdict": "good", "category": "routing"})
    labels = client.get("/api/labels").json()
    assert len(labels) == 2
    assert next(l for l in labels if l["turn"] == 1)["category"] is None   # good labels carry no category
    assert client.post("/api/labels", json={**bad, "turn": 99}).status_code == 404
    assert client.post("/api/labels", json={**bad, "verdict": "meh"}).status_code == 422

    rows = client.get("/api/traces", params={"only": "bad"}).json()
    assert [r["trace_id"] for r in rows] == [b["trace_id"]]
    assert client.get("/api/stats").json()["labels"]["bad_by_category"] == {"retrieval": 1}

    lines = client.get("/api/labels/export").text.strip().splitlines()
    assert len(lines) == 1
    case = json.loads(lines[0])
    assert case["turns"] == ["noise cancelling headphones under $150 to Germany", "something cheaper"]
    assert case["expect"] == {"behavior": "suggest earbuds"} and case["observed"]["path"][0] == "intake"
    assert len(client.get("/api/labels/export", params={"verdict": "all"}).text.strip().splitlines()) == 2

    assert client.delete(f"/api/labels/{sid}/2").status_code == 200
    assert client.delete(f"/api/labels/{sid}/2").status_code == 404


def test_admin_token_gates_console_but_not_shopper(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "s3cret")
    s = Settings(llm_mode="mock", db_path=":memory:", trace_jsonl=str(tmp_path / "t.jsonl"))
    c = TestClient(create_app(Agent(s, [MockLLM()], store=SessionStore(":memory:"))))
    a = chat(c, "hi")
    assert c.get(f"/api/sessions/{a['session_id']}").status_code == 200   # the PWA restores chats with this
    assert c.get("/api/traces").status_code == 401
    assert c.post("/api/admin/breakers/tool:catalog/open").status_code == 401
    assert c.get("/api/traces", headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert c.get("/api/traces", headers={"X-Admin-Token": "s3cret"}).status_code == 200
    assert c.get("/api/health").json()["admin_auth"] is True


def test_chaos_disabled_blocks_admin(tmp_path, monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_CHAOS", "0")
    s = Settings(llm_mode="mock", db_path=":memory:", trace_jsonl=str(tmp_path / "t.jsonl"))
    c = TestClient(create_app(Agent(s, [MockLLM()], store=SessionStore(":memory:"))))
    assert c.post("/api/admin/faults/tool:catalog", json={"down": True}).status_code == 403
    assert c.delete("/api/admin/faults").status_code == 403   # used to be unguarded


def test_pii_masking_keeps_trace_jsonl_valid(tmp_path):
    t = Trace("s_x", 1, "r_1")
    t.event("guard", "input", note="call me on +1 415 555 0100 or a@b.com")
    path = tmp_path / "t.jsonl"
    write_jsonl(t, path)
    rec = json.loads(path.read_text())                       # float timestamps used to become [phone]
    assert isinstance(rec["started_at"], float)
    assert rec["spans"][0]["attrs"]["note"] == "call me on [phone] or [email]"


def test_console_pii_masked(client):
    a = chat(client, "my email is jane.doe@example.com, my parcel never arrived order #AB12345")
    row = client.get("/api/traces").json()[0]
    assert "jane.doe@example.com" not in row["user_message"] and "[email]" in row["user_message"]
    d = client.get(f"/api/traces/{a['trace_id']}").json()
    assert "jane.doe@example.com" not in json.dumps(d)
