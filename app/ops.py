"""Ops console backend: trace summaries, aggregate stats, badcase labels, eval export.

Everything here is read-side over the `turns` table written by SessionStore, plus one
table of its own (`labels`). Labels are keyed by (session_id, turn): re-labelling a turn
overwrites the previous label rather than piling up duplicates.

Text shown to operators (user message, reply) is PII-masked on the way out, the same
masking the JSONL trace log uses.
"""
from __future__ import annotations

import json
import time
from typing import Literal

from pydantic import BaseModel, Field

from .harness.pii import mask_obj, mask_pii
from .harness.state import SessionStore

VERDICTS = ("good", "bad")
CATEGORIES = ("routing", "slots", "retrieval", "generation", "guard", "tool", "harness", "ux", "other")
FILTERS = ("all", "degraded", "errors", "guard", "unlabelled", "bad")


class LabelIn(BaseModel):
    session_id: str = Field(min_length=3, max_length=64)
    turn: int = Field(ge=1)
    verdict: Literal["good", "bad"]
    category: Literal["routing", "slots", "retrieval", "generation", "guard", "tool", "harness", "ux", "other"] | None = None
    note: str = Field(default="", max_length=2000)
    expected_intent: str | None = Field(default=None, max_length=40)
    expected_behavior: str = Field(default="", max_length=2000)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS labels (
  session_id TEXT NOT NULL, turn INTEGER NOT NULL, trace_id TEXT, verdict TEXT NOT NULL, category TEXT,
  note TEXT, expected_intent TEXT, expected_behavior TEXT, prompt_version TEXT,
  created_at REAL, updated_at REAL, PRIMARY KEY (session_id, turn));
"""
_LABEL_COLS = ["session_id", "turn", "trace_id", "verdict", "category", "note", "expected_intent",
               "expected_behavior", "prompt_version", "created_at", "updated_at"]


class LabelError(Exception):
    pass


class OpsStore:
    def __init__(self, store: SessionStore):
        self.store = store
        self._c, self._lock = store._conn, store._lock
        self._c.executescript(_SCHEMA)

    # ---- turns ---------------------------------------------------------------
    def _rows(self, where: str = "", params: tuple = (), limit: int = 50) -> list[tuple]:
        sql = ("SELECT session_id, turn, user_message, response_json, trace_json, created_at FROM turns "
               f"{where} ORDER BY created_at DESC LIMIT ?")
        with self._lock:
            return self._c.execute(sql, (*params, limit)).fetchall()

    def _labels_for(self, keys: list[tuple[str, int]]) -> dict[tuple[str, int], dict]:
        if not keys:
            return {}
        sids = sorted({k[0] for k in keys})
        with self._lock:
            rows = self._c.execute(f"SELECT {', '.join(_LABEL_COLS)} FROM labels WHERE session_id IN "
                                   f"({','.join('?' * len(sids))})", sids).fetchall()
        out = {}
        for r in rows:
            d = dict(zip(_LABEL_COLS, r))
            out[(d["session_id"], d["turn"])] = d
        return out

    def traces(self, limit: int = 50, session_id: str | None = None, q: str | None = None,
               only: str = "all") -> list[dict]:
        where, params = [], []
        if session_id:
            where.append("session_id = ?")
            params.append(session_id)
        if q:
            where.append("LOWER(user_message) LIKE ?")
            params.append(f"%{q.lower()}%")
        # Filters on derived fields run in Python, so over-fetch to still fill the page.
        fetch = limit if only == "all" else min(limit * 10, 2000)
        rows = self._rows(("WHERE " + " AND ".join(where)) if where else "", tuple(params), fetch)
        labels = self._labels_for([(r[0], r[1]) for r in rows])
        out = []
        for r in rows:
            s = summarize(r, labels.get((r[0], r[1])))
            if _keep(s, only):
                out.append(s)
            if len(out) >= limit:
                break
        return out

    def trace(self, trace_id: str) -> dict | None:
        with self._lock:
            r = self._c.execute(
                "SELECT session_id, turn, user_message, response_json, trace_json, created_at FROM turns "
                "WHERE json_extract(trace_json, '$.trace_id') = ?", (trace_id,)).fetchone()
        if not r:
            return None
        label = self._labels_for([(r[0], r[1])]).get((r[0], r[1]))
        return {**summarize(r, label), "trace": mask_obj(json.loads(r[4])), "response": mask_obj(json.loads(r[3]))}

    def stats(self, limit: int = 200) -> dict:
        rows = self._rows(limit=limit)
        labels = self._labels_for([(r[0], r[1]) for r in rows])
        ss = [summarize(r, labels.get((r[0], r[1]))) for r in rows]
        n = len(ss)
        lat = sorted(s["duration_ms"] for s in ss)
        cost = sum(s["usage"].get("cost_usd", 0.0) for s in ss)
        deg: dict[str, int] = {}
        intents: dict[str, int] = {}
        cats: dict[str, int] = {}
        for s in ss:
            for d in s["degradations"]:
                deg[d] = deg.get(d, 0) + 1
            intents[s["intent"] or "none"] = intents.get(s["intent"] or "none", 0) + 1
            if s["label"] and s["label"]["verdict"] == "bad":
                c = s["label"]["category"] or "other"
                cats[c] = cats.get(c, 0) + 1
        rate = lambda k: round(sum(1 for s in ss if k(s)) / n, 4) if n else 0.0  # noqa: E731
        return {
            "window": n,
            "sessions": len({s["session_id"] for s in ss}),
            "degraded_rate": rate(lambda s: s["degradations"]),
            "error_rate": rate(lambda s: s["errors"]),
            "guard_block_rate": rate(lambda s: s["guard_blocks"]),
            "retry_rate": rate(lambda s: s["retries"]),
            "latency_ms": {"p50": _pct(lat, 0.5), "p95": _pct(lat, 0.95), "max": lat[-1] if lat else 0.0},
            "cost_usd": {"total": round(cost, 6), "per_turn": round(cost / n, 6) if n else 0.0},
            "tokens": {"input": sum(s["usage"].get("input_tokens", 0) for s in ss),
                       "output": sum(s["usage"].get("output_tokens", 0) for s in ss)},
            "degradations": dict(sorted(deg.items(), key=lambda kv: -kv[1])),
            "intents": dict(sorted(intents.items(), key=lambda kv: -kv[1])),
            "labels": {"good": sum(1 for s in ss if s["label"] and s["label"]["verdict"] == "good"),
                       "bad": sum(1 for s in ss if s["label"] and s["label"]["verdict"] == "bad"),
                       "bad_by_category": dict(sorted(cats.items(), key=lambda kv: -kv[1]))},
        }

    # ---- labels --------------------------------------------------------------
    def upsert_label(self, body: LabelIn, prompt_version: str) -> dict:
        with self._lock:
            row = self._c.execute("SELECT trace_json FROM turns WHERE session_id=? AND turn=?",
                                  (body.session_id, body.turn)).fetchone()
            if not row:
                raise LabelError("unknown session/turn")
            trace_id = json.loads(row[0]).get("trace_id")
            now = time.time()
            category = body.category if body.verdict == "bad" else None
            if body.verdict == "bad" and not category:
                category = "other"
            self._c.execute(
                "INSERT INTO labels VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(session_id, turn) DO UPDATE SET "
                "verdict=excluded.verdict, category=excluded.category, note=excluded.note, "
                "expected_intent=excluded.expected_intent, expected_behavior=excluded.expected_behavior, "
                "prompt_version=excluded.prompt_version, updated_at=excluded.updated_at",
                (body.session_id, body.turn, trace_id, body.verdict, category, body.note.strip(),
                 body.expected_intent or None, body.expected_behavior.strip(), prompt_version, now, now))
        return self.label(body.session_id, body.turn)

    def label(self, session_id: str, turn: int) -> dict | None:
        with self._lock:
            r = self._c.execute(f"SELECT {', '.join(_LABEL_COLS)} FROM labels WHERE session_id=? AND turn=?",
                                (session_id, turn)).fetchone()
        return dict(zip(_LABEL_COLS, r)) if r else None

    def delete_label(self, session_id: str, turn: int) -> bool:
        with self._lock:
            return self._c.execute("DELETE FROM labels WHERE session_id=? AND turn=?", (session_id, turn)).rowcount == 1

    def labels(self, verdict: str | None = None, limit: int = 500) -> list[dict]:
        sql = f"SELECT {', '.join(_LABEL_COLS)} FROM labels"
        params: tuple = ()
        if verdict:
            sql += " WHERE verdict=?"
            params = (verdict,)
        with self._lock:
            rows = self._c.execute(sql + " ORDER BY updated_at DESC LIMIT ?", (*params, limit)).fetchall()
        return [dict(zip(_LABEL_COLS, r)) for r in rows]

    def export_cases(self, verdict: str | None = "bad") -> list[dict]:
        """Labelled turns -> eval cases for the M5 runner.

        A case replays the conversation up to the labelled turn (all user messages, in order)
        and asserts on the labelled turn only. `observed` is what the agent did when labelled,
        so a regression report can say "fixed" vs "still failing".
        """
        cases = []
        for lb in self.labels(verdict=verdict, limit=10_000):
            with self._lock:
                rows = self._c.execute(
                    "SELECT turn, user_message, response_json, trace_json FROM turns WHERE session_id=? AND turn<=? "
                    "ORDER BY turn", (lb["session_id"], lb["turn"])).fetchall()
            if not rows or rows[-1][0] != lb["turn"]:
                continue
            resp, tr = json.loads(rows[-1][2]), json.loads(rows[-1][3])
            cases.append({
                "id": f"lbl_{lb['session_id']}_{lb['turn']}",
                "source": "console_label",
                "verdict": lb["verdict"],
                "category": lb["category"],
                "note": lb["note"],
                "turns": [mask_pii(r[1] or "") for r in rows],
                "expect": {k: v for k, v in {"intent": lb["expected_intent"],
                                             "behavior": lb["expected_behavior"]}.items() if v},
                "observed": {"intent": resp.get("intent"), "path": tr.get("path", []),
                             "degradations": tr.get("degradations", []),
                             "reply": mask_pii(_reply_text(resp))},
                "prompt_version": lb["prompt_version"],
            })
        return cases


# ---- helpers ----------------------------------------------------------------
def summarize(row: tuple, label: dict | None) -> dict:
    session_id, turn, user_message, response_json, trace_json, created_at = row
    tr, resp = json.loads(trace_json), json.loads(response_json)
    spans = tr.get("spans", [])
    retries = sum(max(0, len(s.get("attrs", {}).get("attempts", []) or []) - 1) for s in spans)
    return {
        "trace_id": tr.get("trace_id"),
        "session_id": session_id,
        "turn": turn,
        "started_at": tr.get("started_at", created_at),
        "duration_ms": tr.get("duration_ms", 0.0),
        "user_message": mask_pii(user_message or ""),
        "reply": mask_pii(_reply_text(resp))[:400],
        "intent": resp.get("intent"),
        "path": tr.get("path", []),
        "degradations": tr.get("degradations", []),
        "usage": tr.get("usage", {}),
        "meta": tr.get("meta", {}),
        "spans": len(spans),
        "retries": retries,
        "errors": sum(1 for s in spans if s.get("status") == "error"),
        "guard_blocks": sum(1 for s in spans if s.get("kind") == "guard" and s.get("status") == "blocked"),
        "label": label,
    }


def _keep(s: dict, only: str) -> bool:
    return {
        "all": True,
        "degraded": bool(s["degradations"]),
        "errors": s["errors"] > 0,
        "guard": s["guard_blocks"] > 0,
        "unlabelled": s["label"] is None,
        "bad": bool(s["label"] and s["label"]["verdict"] == "bad"),
    }.get(only, True)


def _reply_text(resp: dict) -> str:
    for m in resp.get("messages", []):
        if m.get("type") == "text":
            return m.get("text", "")
    return ""


def _pct(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    i = min(len(sorted_vals) - 1, max(0, round(q * (len(sorted_vals) - 1))))
    return round(sorted_vals[i], 1)
