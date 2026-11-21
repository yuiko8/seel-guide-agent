"""HTTP API for the PWA and the internal ops console.

Concurrency: one in-flight turn per session (per-session lock -> 409 if busy),
plus optimistic locking in the store as a second line of defence.
Idempotency: the client sends a request_id per message; a retry with the same id
returns the stored response instead of running the turn again.
"""
from __future__ import annotations

import os
import threading

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent.graph import Agent
from .agent.prompts import PROMPT_VERSION
from .config import ROOT, Settings
from .harness.faults import Fault, FaultInjector
from .harness.state import ConcurrentModification
from .llm.mock import MockLLM


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
    error_rate: float = 0.0
    timeout_rate: float = 0.0
    malformed_rate: float = 0.0
    down: bool = False
    latency_ms: float = 0.0


def create_app(agent: Agent | None = None) -> FastAPI:
    app = FastAPI(title="Cross-border Shopping Agent", version="0.1")
    agent = agent or build_agent()
    app.state.agent = agent
    locks: dict[str, threading.Lock] = {}
    locks_guard = threading.Lock()
    chaos_enabled = os.getenv("ADMIN_CHAOS", "1") == "1"

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
                "faults": {k: v.__dict__ for k, v in agent.gateway.faults.faults.items()},
                "chaos_enabled": chaos_enabled}

    @app.get("/api/sessions/{sid}")
    def session(sid: str):
        st = agent.store.load(sid)
        if not st:
            raise HTTPException(404, "unknown session")
        return {"state": st.model_dump(), "turns": agent.store.turns(sid)}

    @app.get("/api/traces")
    def traces(limit: int = 50):
        return agent.store.recent_traces(min(limit, 200))

    @app.get("/api/tickets")
    def tickets(limit: int = 50):
        return agent.tickets.list(min(limit, 200))

    @app.post("/api/admin/faults/{dependency}")
    def set_fault(dependency: str, body: FaultIn):
        if not chaos_enabled:
            raise HTTPException(403, "chaos disabled")
        agent.gateway.faults.faults[dependency] = Fault(**body.model_dump())
        return {"ok": True, "faults": {k: v.__dict__ for k, v in agent.gateway.faults.faults.items()}}

    @app.delete("/api/admin/faults")
    def clear_faults():
        agent.gateway.faults.faults.clear()
        return {"ok": True}

    @app.post("/api/admin/breakers/{name}/{state}")
    def force_breaker(name: str, state: str):
        if not chaos_enabled or state not in ("open", "closed"):
            raise HTTPException(400, "bad request")
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

