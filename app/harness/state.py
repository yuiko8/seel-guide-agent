"""Session state + durable store.

- SessionState is the single source of truth for a conversation. Slots (budget,
  country, recipient...) live here, NOT only in the chat history, so context
  compaction can never "forget" them.
- Every completed turn writes a snapshot (state, response, trace) keyed by
  (session_id, turn). This gives replay, audit, and request-level idempotency:
  a retried request_id returns the stored response instead of re-running the turn.
- Optimistic concurrency: save() is a compare-and-swap on `version`; two
  concurrent turns on one session cannot silently overwrite each other.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


class Slots(BaseModel):
    budget_max: float | None = None
    budget_min: float | None = None
    country: str | None = None          # ISO-3166 alpha-2
    recipient: str | None = None
    category: str | None = None         # electronics | apparel | home | beauty | outdoor
    keywords: list[str] = Field(default_factory=list)

    def filled(self) -> dict[str, Any]:
        return {k: v for k, v in self.model_dump().items() if v not in (None, [], "")}


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    turn: int


class SessionState(BaseModel):
    session_id: str
    version: int = 0
    turn: int = 0
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    status: Literal["active", "handoff"] = "active"
    slots: Slots = Field(default_factory=Slots)
    slot_turns: dict[str, int] = Field(default_factory=dict)   # slot -> turn last set (for drift debugging)
    messages: list[Message] = Field(default_factory=list)
    summary: str = ""
    summarized_upto_turn: int = 0
    clarify_streak: int = 0
    last_clarify: str | None = None
    pending: dict | None = None        # question parked while we asked a clarifying question {intent, question}
    last_node: str | None = None
    last_intent: str | None = None
    candidates: list[str] = Field(default_factory=list)        # SKUs shown last
    quotes: dict[str, dict] = Field(default_factory=dict)
    tickets: list[dict] = Field(default_factory=list)

    @classmethod
    def new(cls) -> "SessionState":
        return cls(session_id="s_" + uuid.uuid4().hex[:12])


class ConcurrentModification(Exception):
    pass


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY, version INTEGER NOT NULL, state_json TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS turns (
  session_id TEXT NOT NULL, turn INTEGER NOT NULL, request_id TEXT NOT NULL,
  user_message TEXT, response_json TEXT, state_json TEXT, trace_json TEXT, created_at REAL,
  PRIMARY KEY (session_id, turn));
CREATE UNIQUE INDEX IF NOT EXISTS turns_req ON turns(session_id, request_id);
CREATE TABLE IF NOT EXISTS idempotency (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, created_at REAL);
"""


class SessionStore:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._lock = threading.RLock()

    # ---- sessions -------------------------------------------------------------
    def load(self, session_id: str) -> SessionState | None:
        with self._lock:
            row = self._conn.execute("SELECT state_json FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        return SessionState.model_validate_json(row[0]) if row else None

    def save(self, state: SessionState, expected_version: int) -> SessionState:
        state = state.model_copy(deep=True)
        state.version = expected_version + 1
        state.updated_at = time.time()
        blob = state.model_dump_json()
        with self._lock:
            if expected_version == 0:
                try:
                    self._conn.execute("INSERT INTO sessions VALUES (?,?,?,?)", (state.session_id, state.version, blob, state.updated_at))
                    return state
                except sqlite3.IntegrityError:
                    pass  # exists: fall through to CAS update
            cur = self._conn.execute(
                "UPDATE sessions SET version=?, state_json=?, updated_at=? WHERE session_id=? AND version=?",
                (state.version, blob, state.updated_at, state.session_id, expected_version))
            if cur.rowcount != 1:
                raise ConcurrentModification(f"session {state.session_id} changed (expected v{expected_version})")
        return state

    # ---- turns / replay -----------------------------------------------------
    def record_turn(self, state: SessionState, request_id: str, user_message: str, response: dict, trace: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO turns VALUES (?,?,?,?,?,?,?,?)",
                (state.session_id, state.turn, request_id, user_message, json.dumps(response, ensure_ascii=False),
                 state.model_dump_json(), json.dumps(trace, ensure_ascii=False, default=str), time.time()))

    def find_by_request(self, session_id: str, request_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT response_json FROM turns WHERE session_id=? AND request_id=?",
                                     (session_id, request_id)).fetchone()
        return json.loads(row[0]) if row else None

    def turns(self, session_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT turn, request_id, user_message, response_json, trace_json, created_at FROM turns WHERE session_id=? ORDER BY turn",
                (session_id,)).fetchall()
        return [{"turn": r[0], "request_id": r[1], "user_message": r[2], "response": json.loads(r[3]),
                 "trace": json.loads(r[4]), "created_at": r[5]} for r in rows]

    def state_at(self, session_id: str, turn: int) -> SessionState | None:
        with self._lock:
            row = self._conn.execute("SELECT state_json FROM turns WHERE session_id=? AND turn=?", (session_id, turn)).fetchone()
        return SessionState.model_validate_json(row[0]) if row else None

    def recent_traces(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT trace_json FROM turns ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    # ---- idempotency (side-effecting tools) --------------------------------------
    def get(self, key: str) -> Any | None:
        with self._lock:
            row = self._conn.execute("SELECT value_json FROM idempotency WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute("INSERT OR IGNORE INTO idempotency VALUES (?,?,?)",
                               (key, json.dumps(value, default=str), time.time()))
