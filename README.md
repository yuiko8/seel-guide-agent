# Atlas — cross-border shopping agent (RAG + Agent + Harness)

A minimal but production-shaped shopping assistant for international shoppers: product picks with
delivery time and import taxes up front, built around a **harness layer** (state, fault tolerance,
circuit breaking, context management, guardrails, tracing, chaos evals).

All merchants, prices, customs numbers and protection terms are **simulated sandbox data**.

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install fastapi uvicorn pydantic anthropic rank-bm25 pytest httpx
.venv/bin/python scripts/gen_data.py          # regenerate the simulated knowledge base
.venv/bin/uvicorn app.api:create_app --factory --port 8000
```

Open http://localhost:8000 on a phone-sized window (installable PWA). Ops console (M4): `/console`.

- `LLM_MODE=mock` (default when no `ANTHROPIC_API_KEY`): deterministic rule-based model, zero cost.
- `LLM_MODE=claude`: primary `claude-opus-5-5`, fallback `claude-sonnet-5-5`, then rules.
- `SIM_TODAY=2026-11-21` pins the date (peak-season shipping rules) for reproducible runs.

## Test

```bash
.venv/bin/python -m pytest -q
```

## Layout

```
app/harness/   errors, deadline, retry, circuit_breaker, gateway, faults, state, context, guardrails, tracing, pii
app/agent/     graph (state machine), llm_chain (model fallback), rules (router/template = degrade path), prompts, schemas
app/llm/       Claude client, MockLLM
app/tools/     catalog search, restrictions, shipping & landed cost
app/api.py     FastAPI: /api/chat, /api/health, /api/sessions/{id}, /api/traces, /api/admin/faults
mobile/        PWA (no build step)
data/          simulated catalog, merchant policies, cross-border rules, protection terms
```
