"""Support ticket tool (the hand-off to a human / Support agent).

Side-effecting, so:
  - the gateway calls it with an idempotency key -> a retried call returns the same ticket
  - the service itself also dedupes: one OPEN ticket per (session, issue, order) -> the
    shopper complaining twice doesn't create two tickets
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid

from pydantic import BaseModel, Field

from ..harness.pii import mask_pii

ISSUE_TYPES = ["lost", "damaged", "late", "wrong_item", "refund", "other"]
PRIORITY = {"lost": "high", "damaged": "high", "wrong_item": "high", "late": "normal", "refund": "normal", "other": "normal"}

ISSUE_PATTERNS = [
    ("lost", r"\b(lost|never (arrived|came)|didn'?t (arrive|come)|hasn'?t arrived|not arrived|missing|stolen)\b"),
    ("damaged", r"\b(damaged|broken|cracked|smashed|shattered|torn|crushed|defective|stopped working)\b"),
    ("wrong_item", r"\b(wrong (item|size|color|colour|product)|not what i ordered)\b"),
    ("late", r"\b(late|delayed|still waiting|taking (so|too) long|stuck)\b"),
    ("refund", r"\b(refund|money back|charged twice|cancel)\b"),
]
ORDER_ID_RE = re.compile(r"(?:order\s*(?:number|no\.?|#)?\s*|#)([A-Z0-9]{5,12})\b", re.I)


def classify_issue(text: str) -> str:
    for kind, pat in ISSUE_PATTERNS:
        if re.search(pat, text, re.I):
            return kind
    return "other"


def extract_order_id(text: str) -> str | None:
    m = ORDER_ID_RE.search(text)
    if m and re.search(r"\d", m.group(1)):
        return m.group(1).upper()
    return None


class TicketInput(BaseModel):
    session_id: str = Field(min_length=3, max_length=64)
    issue_type: str
    summary: str = Field(min_length=5, max_length=2000)
    order_id: str | None = Field(default=None, max_length=20)
    country: str | None = None
    skus: list[str] = Field(default_factory=list)
    request_id: str | None = Field(default=None, max_length=64)


class TicketService:
    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self._c, self._lock = conn, lock
        self._c.execute("""CREATE TABLE IF NOT EXISTS tickets (
            ticket_id TEXT PRIMARY KEY, session_id TEXT, issue_type TEXT, order_id TEXT, status TEXT,
            priority TEXT, summary TEXT, payload TEXT, created_at REAL, request_id TEXT)""")

    def create_ticket(self, session_id: str, issue_type: str, summary: str, order_id: str | None = None,
                      country: str | None = None, skus: list[str] | None = None, request_id: str | None = None) -> dict:
        issue_type = issue_type if issue_type in ISSUE_TYPES else "other"
        with self._lock:
            row = self._c.execute(
                "SELECT ticket_id, priority, created_at, request_id FROM tickets WHERE session_id=? AND issue_type=? "
                "AND IFNULL(order_id,'')=IFNULL(?, '') AND status='open'", (session_id, issue_type, order_id)).fetchone()
            if row:
                # Same request retried (e.g. response lost after commit) -> it's still "the ticket we just opened".
                same_request = request_id is not None and row[3] == request_id
                return {"ticket_id": row[0], "status": "open", "priority": row[1], "deduplicated": not same_request,
                        "issue_type": issue_type, "order_id": order_id}
            tid = "T-" + uuid.uuid4().hex[:6].upper()
            prio = PRIORITY[issue_type]
            payload = {"country": country, "skus": skus or []}
            self._c.execute("INSERT INTO tickets VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (tid, session_id, issue_type, order_id, "open", prio, mask_pii(summary),
                             json.dumps(payload), time.time(), request_id))
        return {"ticket_id": tid, "status": "open", "priority": prio, "deduplicated": False,
                "issue_type": issue_type, "order_id": order_id}

    def list(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._c.execute("SELECT ticket_id, session_id, issue_type, order_id, status, priority, summary, created_at "
                                   "FROM tickets ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        keys = ["ticket_id", "session_id", "issue_type", "order_id", "status", "priority", "summary", "created_at"]
        return [dict(zip(keys, r)) for r in rows]
