"""Retrieval eval: Recall@k and MRR on a labeled query set, with per-signal ablation
(BM25 only / char-ngram only / hybrid) so changes to retrieval are measured, not guessed.

Usage: python -m evals.retrieval_eval [--k 5] [--save]
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from app.rag.retriever import HybridRetriever, expand, tokenize, char_ngrams

HERE = Path(__file__).parent


def load(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def ranked_ids(r: HybridRetriever, query: str, filters: dict | None, mode: str, n: int = 10) -> list[str]:
    if mode == "hybrid":
        return [h.chunk.id for h in r.search(query, k=n, filters=filters, min_bm25=-1)["hits"]]
    q = expand(query)
    idx = [i for i, c in enumerate(r.chunks) if not filters or all(
        (c.meta.get(k) in v) if isinstance(v, list) else c.meta.get(k) == v for k, v in filters.items())]
    if mode == "bm25":
        s = r.bm25.get_scores(tokenize(q))
    else:
        s = r.mat @ r._vec(char_ngrams(q))
    return [r.chunks[i].id for i in sorted(idx, key=lambda i: -s[i])[:n]]


def evaluate(cases: list[dict], k: int = 5) -> dict:
    r = HybridRetriever()
    out = {}
    for mode in ("bm25", "char", "hybrid"):
        rec1 = reck = mrr = 0.0
        misses = []
        for c in cases:
            ids = ranked_ids(r, c["query"], c.get("filters"), mode)
            rel = set(c["relevant"])
            first = next((i for i, x in enumerate(ids) if x in rel), None)
            rec1 += 1.0 if ids[:1] and ids[0] in rel else 0.0
            reck += len(rel & set(ids[:k])) / len(rel)
            mrr += 1 / (first + 1) if first is not None else 0
            if first is None or first >= k:
                misses.append({"id": c["id"], "query": c["query"], "got": ids[:3], "want": c["relevant"]})
        n = len(cases)
        out[mode] = {"recall@1": round(rec1 / n, 3), f"recall@{k}": round(reck / n, 3), "mrr": round(mrr / n, 3),
                     "misses": misses}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--set", default="retrieval", help="retrieval | retrieval_holdout")
    a = ap.parse_args()
    cases = load(HERE / "datasets" / f"{a.set}.jsonl")
    res = evaluate(cases, a.k)
    print(f"{len(cases)} queries")
    print(f"{'mode':8} {'R@1':>6} {'R@' + str(a.k):>6} {'MRR':>6}")
    for m, v in res.items():
        print(f"{m:8} {v['recall@1']:6.3f} {v[f'recall@{a.k}']:6.3f} {v['mrr']:6.3f}")
    for miss in res["hybrid"]["misses"]:
        print("  MISS", miss)
    if a.save:
        p = HERE / "reports" / f"{a.set}-{time.strftime('%Y%m%d-%H%M%S')}.json"
        p.write_text(json.dumps({"n": len(cases), "k": a.k, "results": res}, indent=1))
        print("saved", p)


if __name__ == "__main__":
    main()
