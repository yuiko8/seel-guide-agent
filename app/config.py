from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


@dataclass
class Settings:
    llm_mode: str = field(default_factory=lambda: os.getenv("LLM_MODE", "claude" if os.getenv("ANTHROPIC_API_KEY") else "mock"))
    primary_model: str = field(default_factory=lambda: os.getenv("PRIMARY_MODEL", "claude-opus-5-5"))
    fallback_model: str = field(default_factory=lambda: os.getenv("FALLBACK_MODEL", "claude-sonnet-5-5"))
    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", str(ROOT / "var" / "app.db")))
    trace_jsonl: str = field(default_factory=lambda: os.getenv("TRACE_JSONL", str(ROOT / "var" / "traces.jsonl")))
    turn_budget_s: float = field(default_factory=lambda: _f("TURN_BUDGET_S", 30.0))
    llm_timeout_s: float = field(default_factory=lambda: _f("LLM_TIMEOUT_S", 20.0))
    tool_timeout_s: float = field(default_factory=lambda: _f("TOOL_TIMEOUT_S", 2.0))
    max_steps: int = 8
    max_clarify_streak: int = 2
    sim_today: str | None = field(default_factory=lambda: os.getenv("SIM_TODAY"))  # pin the date for evals
