"""Per-turn trace: a flat list of spans (node, tool, llm, guard, breaker, degrade).

A trace is the unit of badcase analysis: given a session_id + turn, you can see
which node ran, which dependency failed, how many retries happened, what got
degraded and what it cost. Persisted to SQLite by the SessionStore and mirrored
to JSONL for grep-ability.
"""
from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .pii import mask_obj


@dataclass
class Span:
    kind: str            # node | tool | llm | retrieval | guard | degrade | breaker | context
    name: str
    start_ms: float
    duration_ms: float = 0.0
    status: str = "ok"   # ok | error | skipped | degraded | blocked
    attrs: dict[str, Any] = field(default_factory=dict)


class Trace:
    def __init__(self, session_id: str, turn: int, request_id: str):
        self.trace_id = uuid.uuid4().hex[:16]
        self.session_id = session_id
        self.turn = turn
        self.request_id = request_id
        self.t0 = time.perf_counter()
        self.started_at = time.time()
        self.spans: list[Span] = []
        self._open: list[Span] = []      # stack of spans currently open (innermost last)
        self.path: list[str] = []
        self.degradations: list[str] = []
        self.meta: dict[str, Any] = {}   # prompt_version, llm_mode... (what produced this turn)
        self.usage = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "llm_calls": 0}

    def _now_ms(self) -> float:
        return (time.perf_counter() - self.t0) * 1000

    @contextmanager
    def span(self, kind: str, name: str, **attrs):
        s = Span(kind=kind, name=name, start_ms=round(self._now_ms(), 2), attrs=dict(attrs))
        self.spans.append(s)
        self._open.append(s)
        try:
            yield s
        except BaseException as e:
            s.status = "error"
            s.attrs["error"] = f"{type(e).__name__}: {e}"[:300]
            raise
        finally:
            s.duration_ms = round(self._now_ms() - s.start_ms, 2)
            if self._open and self._open[-1] is s:
                self._open.pop()

    def event(self, kind: str, name: str, status: str = "ok", **attrs) -> None:
        self.spans.append(Span(kind=kind, name=name, start_ms=round(self._now_ms(), 2), status=status, attrs=attrs))

    def degrade(self, mode: str, reason: str) -> None:
        if mode not in self.degradations:
            self.degradations.append(mode)
        self.event("degrade", mode, status="degraded", reason=reason)

    def add_usage(self, input_tokens: int, output_tokens: int, cost_usd: float, model: str | None = None) -> None:
        # Attribute usage to the innermost open LLM span too, so the console can show cost per call.
        for sp in reversed(self._open):
            if sp.kind == "llm":
                a = sp.attrs
                a["input_tokens"] = a.get("input_tokens", 0) + input_tokens
                a["output_tokens"] = a.get("output_tokens", 0) + output_tokens
                a["cost_usd"] = round(a.get("cost_usd", 0.0) + cost_usd, 6)
                if model:
                    a["model"] = model
                break
        self.usage["input_tokens"] += input_tokens
        self.usage["output_tokens"] += output_tokens
        self.usage["cost_usd"] = round(self.usage["cost_usd"] + cost_usd, 6)
        self.usage["llm_calls"] += 1

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "turn": self.turn,
            "request_id": self.request_id,
            "started_at": self.started_at,
            "duration_ms": round(self._now_ms(), 2),
            "meta": self.meta,
            "path": self.path,
            "degradations": self.degradations,
            "usage": self.usage,
            "spans": [asdict(s) for s in self.spans],
        }


def write_jsonl(trace: Trace, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(mask_obj(trace.to_dict()), ensure_ascii=False, default=str) + "\n")
