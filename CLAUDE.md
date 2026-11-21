# Project context for Claude

Cross-border shopping agent (RAG + Agent + **Harness**) modelled on Seel's post-purchase protection
business, used by the owner to simulate the day-to-day work of an AI product intern at Seel.
The owner writes in Chinese; reply in Chinese. The shopper-facing app and code are in English.

Read `PLAN.md` (plan + confirmed decisions) and `docs/reports/M2-rag.md`, `docs/reports/M3-protection-handoff.md`,
`docs/reports/M4-console.md`, `docs/reports/M5-evals.md`
before making changes.

## Confirmed decisions
- App: mobile-first PWA in `mobile/` (plain JS, no build step). Backend: FastAPI (`app/api.py`, factory `create_app`).
- LLM: primary `claude-opus-5-5`, fallback `claude-sonnet-5-5`, then rule router/templates (`app/agent/rules.py`).
  `LLM_MODE=claude` automatically when `ANTHROPIC_API_KEY` is set; `MockLLM` otherwise (CI must stay deterministic).
- Focus: **harness depth** (state, retries, breakers, degrade paths, context budget, guardrails, tracing, chaos).
- Principle: **the LLM writes prose, code owns facts.** Prices, taxes, delivery windows, quotes, policy text come
  from tools/retrieval; UI cards render those structures; output guards check numbers and citations.
- All data in `data/` is simulated; never use Seel's brand or real terms.

## Status
- M1 done: harness core, state machine, PWA. M2 done: hybrid RAG + cited answers + grounding guard + retrieval evals.
- M3 done: protection quotes (`app/tools/protection.py`), support hand-off with deduped tickets (`app/tools/tickets.py`).
- M4 done: ops console at `/console` (`console/`, backend `app/ops.py`): trace list/detail with span waterfall,
  stats, breaker/chaos panel, tickets, badcase labels (SQLite `labels`) exported as JSONL eval cases
  (`GET /api/labels/export`). Console endpoints require `X-Admin-Token` when `ADMIN_TOKEN` is set.
  Never run `mask_pii` over serialized JSON; use `mask_obj`.
- M5 done: conversation eval `python -m evals.run` (golden 63 / adversarial 20 / chaos x7 profiles in virtual time /
  red-team liar responder), hard invariants independent of the agent's guard, regression gate vs
  `evals/baseline.json` (`make gate`, CI in `.github/workflows/ci.yml`), E2E `node e2e/app.e2e.js`.
  Golden expectations describe CORRECT behaviour; 13 known failures live in the baseline = M6 backlog.
  After fixing a badcase: `make gate` (newly passing is fine) then `make baseline` and commit the baseline diff.
- **The real Claude API has not been exercised yet** - everything so far ran on MockLLM. First thing in a session
  with `ANTHROPIC_API_KEY`: run the scenarios against the real model and compare with mock (routing accuracy,
  guard violations, latency, cost per turn).

## Next: M6
Intern workflow: PRD, metrics doc, practice task cards, one badcase -> fix -> eval-delta report, weekly template.
Backlog = the 13 failing cases in `evals/baseline.json` (table in `docs/reports/M5-evals.md`), starting with the
planted one: after "something cheaper", no ANC headphones fit the new budget and the agent only says
"nothing found" instead of suggesting earbuds (`ctx_cheaper_suggests_alternative`).

## Commands
```bash
python3 -m venv .venv && .venv/bin/pip install fastapi uvicorn pydantic anthropic rank-bm25 pytest httpx
.venv/bin/python -m pytest -q
.venv/bin/python -m evals.retrieval_eval            # dev set; --set retrieval_holdout is acceptance-only, never tune on it
make gate                                           # conversation eval + regression gate (mock); make e2e for the PWA
SIM_TODAY=2026-11-21 .venv/bin/uvicorn app.api:create_app --factory --port 8000
```

## Conventions
- Every external call goes through `Gateway.call` with a `CallSpec` (breaker + timeout + retry + trace span).
  Side-effecting tools need an idempotency key scoped to the request (`session:request_id`).
- New behaviour needs a test in `tests/` (and a golden/adversarial case if it is user-visible); bump `PROMPT_VERSION` in `app/agent/prompts.py` when prompts change.
- Verify UI changes with Playwright at iPhone 13 size; check there is no horizontal overflow.
