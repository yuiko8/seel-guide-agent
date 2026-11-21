PY ?= .venv/bin/python
NODE_PATH ?= $(shell npm root -g 2>/dev/null)

.PHONY: setup test eval gate baseline eval-claude e2e retrieval ci serve

setup:
	python3 -m venv .venv && .venv/bin/pip install -q fastapi uvicorn pydantic anthropic rank-bm25 pytest httpx

test:
	$(PY) -m pytest -q

retrieval:
	$(PY) -m evals.retrieval_eval

eval:            ## conversation eval (mock LLM): clean + chaos + red team -> evals/reports/conv-mock-latest.md
	$(PY) -m evals.run --quiet && cat evals/reports/conv-mock-latest.md | head -60

gate:            ## eval + regression gate against evals/baseline.json (exit 1 on regression)
	$(PY) -m evals.run --gate --quiet

baseline:        ## accept current results as the new baseline (review the diff before committing!)
	$(PY) -m evals.run --update-baseline --quiet && git --no-pager diff --stat evals/baseline.json

eval-claude:     ## real model, clean runs + judge (needs ANTHROPIC_API_KEY; costs money)
	$(PY) -m evals.run --llm claude --runs clean --judge

e2e:             ## PWA + console E2E at iPhone 13 size (starts its own server)
	NODE_PATH=$(NODE_PATH) node e2e/app.e2e.js

ci: test retrieval gate e2e

serve:
	SIM_TODAY=2026-11-21 $(PY) -m uvicorn app.api:create_app --factory --port 8000
