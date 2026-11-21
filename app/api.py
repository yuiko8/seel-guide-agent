"""HTTP API for the PWA and the internal ops console.

Concurrency: one in-flight turn per session (per-session lock -> 409 if busy),
plus optimistic locking in the store as a second line of defence.
Idempotency: the client sends a request_id per message; a retry with the same id
returns the stored response instead of running the turn again.
"""
from __future__ import annotations

import os
import threading

import hmac
import json

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent.graph import Agent
from .agent.prompts import PROMPT_VERSION
from .agent.schemas import ROUTE_JSON_SCHEMA
from .config import ROOT, Settings
from .harness.faults import Fault, FaultInjector
from .harness.state import ConcurrentModification
from .llm.mock import MockLLM
from .ops import FILTERS, LabelError, LabelIn, OpsStore

# Dependencies the chaos panel can target even before they have been called once.
TOOL_DEPENDENCIES = ["tool:catalog", "tool:shipping", "tool:protection", "tool:tickets", "retrieval:kb"]


def build_agent(settings: Settings | None = None, faults: FaultInjector | None = None) -> Agent:
    s = settings or Settings()
    if s.llm_mode == "claude":
        from .llm.claude import ClaudeClient
        llms = [ClaudeClient(s.primary_model), ClaudeClient(s.fallback_model)]
    else:
        llms = [MockLLM()]
    return Agent(s, llms, faults=faults or FaultInjector())


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = Field(default=None, max_length=64)
    request_id: str | None = Field(default=None, max_length=64)


class FaultIn(BaseModel):
    error_rate: float = Field(default=0.0, ge=0, le=1)
    timeout_rate: float = Field(default=0.0, ge=0, le=1)
    malformed_rate: float = Field(default=0.0, ge=0, le=1)
    down: bool = False
    latency_ms: float = Field(default=0.0, ge=0, le=10_000)


def create_app(agent: Agent | None = None) -> FastAPI:
    app = FastAPI(title="Cross-border Shopping Agent", version="0.1")
    agent = agent or build_agent()
    app.state.agent = agent
    locks: dict[str, threading.Lock] = {}
    locks_guard = threading.Lock()
    chaos_enabled = os.getenv("ADMIN_CHAOS", "1") == "1"
    admin_token = os.getenv("ADMIN_TOKEN", "")
    ops = OpsStore(agent.store)
    app.state.ops = ops

    def require_admin(x_admin_token: str = Header(default="")):
        """Console endpoints expose conversations and can break things: gate them when ADMIN_TOKEN is set."""
        if admin_token and not hmac.compare_digest(x_admin_token, admin_token):
            raise HTTPException(401, "admin token required")

    def require_chaos():
        if not chaos_enabled:
            raise HTTPException(403, "chaos disabled (ADMIN_CHAOS=0)")

    def dependencies() -> list[str]:
        known = [f"llm:{c.model}" for c in agent.chain.clients] + TOOL_DEPENDENCIES
        seen = [b["name"] for b in agent.breakers.snapshot()]
        return known + [d for d in seen if d not in known]

    def faults_view() -> dict:
        f = agent.gateway.faults
        return {k: {**v.__dict__, "injected": f.injected.get(k, 0)} for k, v in f.faults.items()}

    def session_lock(sid: str) -> threading.Lock:
        with locks_guard:
            return locks.setdefault(sid, threading.Lock())

    @app.post("/api/chat")
    def chat(body: ChatIn):
        sid = body.session_id
        lock = session_lock(sid) if sid else None
        if lock and not lock.acquire(blocking=False):
            raise HTTPException(409, "previous message for this session is still being processed")
        try:
            return agent.handle(body.message, session_id=sid, request_id=body.request_id)
        except ConcurrentModification as e:
            raise HTTPException(409, str(e))
        finally:
            if lock:
                lock.release()

    @app.get("/api/health")
    def health():
        return {"llm_mode": agent.s.llm_mode, "models": [c.model for c in agent.chain.clients],
                "prompt_version": PROMPT_VERSION, "breakers": agent.breakers.snapshot(),
                "breaker_events": [e.__dict__ for e in list(agent.breakers.events)[-20:]],
                "faults": faults_view(), "injected": dict(agent.gateway.faults.injected),
                "dependencies": dependencies(), "chaos_enabled": chaos_enabled,
                "admin_auth": bool(admin_token),
                "intents": ROUTE_JSON_SCHEMA["properties"]["intent"]["enum"]}

    @app.get("/api/sessions/{sid}")
    def session(sid: str):
        st = agent.store.load(sid)
        if not st:
            raise HTTPException(404, "unknown session")
        return {"state": st.model_dump(), "turns": agent.store.turns(sid)}

    admin = [Depends(require_admin)]

    @app.get("/api/traces", dependencies=admin)
    def traces(limit: int = 50, session_id: str | None = None, q: str | None = None, only: str = "all"):
        if only not in FILTERS:
            raise HTTPException(400, f"only must be one of {FILTERS}")
        return ops.traces(min(max(limit, 1), 200), session_id=session_id, q=q, only=only)

    @app.get("/api/traces/{trace_id}", dependencies=admin)
    def trace_detail(trace_id: str):
        t = ops.trace(trace_id)
        if not t:
            raise HTTPException(404, "unknown trace")
        return t

    @app.get("/api/stats", dependencies=admin)
    def stats(window: int = 200):
        return ops.stats(min(max(window, 1), 2000))

    @app.get("/api/tickets", dependencies=admin)
    def tickets(limit: int = 50):
        return agent.tickets.list(min(limit, 200))

    # ---- badcase labels (feed the M5 eval set) -------------------------------
    @app.get("/api/labels", dependencies=admin)
    def labels(verdict: str | None = None):
        return ops.labels(verdict=verdict)

    @app.post("/api/labels", dependencies=admin)
    def put_label(body: LabelIn):
        try:
            return ops.upsert_label(body, PROMPT_VERSION)
        except LabelError as e:
            raise HTTPException(404, str(e))

    @app.delete("/api/labels/{sid}/{turn}", dependencies=admin)
    def delete_label(sid: str, turn: int):
        if not ops.delete_label(sid, turn):
            raise HTTPException(404, "no label")
        return {"ok": True}

    @app.get("/api/labels/export", dependencies=admin)
    def export_labels(verdict: str = "bad"):
        cases = ops.export_cases(verdict=None if verdict == "all" else verdict)
        return PlainTextResponse("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cases),
                                 media_type="application/x-ndjson",
                                 headers={"Content-Disposition": 'attachment; filename="labelled_cases.jsonl"'})

    # ---- chaos / breakers ----------------------------------------------------
    @app.post("/api/admin/faults/{dependency}", dependencies=admin + [Depends(require_chaos)])
    def set_fault(dependency: str, body: FaultIn):
        if dependency not in dependencies() and not dependency.endswith("*"):
            raise HTTPException(404, f"unknown dependency {dependency}")
        agent.gateway.faults.faults[dependency] = Fault(**body.model_dump())
        return {"ok": True, "faults": faults_view()}

    @app.delete("/api/admin/faults/{dependency}", dependencies=admin + [Depends(require_chaos)])
    def clear_fault(dependency: str):
        agent.gateway.faults.faults.pop(dependency, None)
        return {"ok": True, "faults": faults_view()}

    @app.delete("/api/admin/faults", dependencies=admin + [Depends(require_chaos)])
    def clear_faults():
        agent.gateway.faults.faults.clear()
        agent.gateway.faults.injected.clear()
        return {"ok": True, "faults": {}}

    @app.post("/api/admin/breakers/{name}/{state}", dependencies=admin + [Depends(require_chaos)])
    def force_breaker(name: str, state: str):
        if state not in ("open", "closed"):
            raise HTTPException(400, "state must be open or closed")
        if name not in dependencies():
            raise HTTPException(404, f"unknown dependency {name}")
        agent.breakers.get(name).force(state)
        return agent.breakers.get(name).snapshot()

    mobile = ROOT / "mobile"
    console = ROOT / "console"
    if console.exists():
        app.mount("/console", StaticFiles(directory=console, html=True), name="console")

    @app.get("/sw.js")
    def sw():
        return FileResponse(mobile / "sw.js", media_type="application/javascript",
                            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})

    if mobile.exists():
        app.mount("/", StaticFiles(directory=mobile, html=True), name="mobile")
    return app

