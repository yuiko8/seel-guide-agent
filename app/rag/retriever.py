"""Hybrid retriever: BM25 (word level) + TF-IDF over character n-grams (robust to
morphology/typos: "returnable" vs "return", "refnd"), fused with Reciprocal Rank
Fusion, then metadata filtering and a confidence floor.

Why not dense embeddings yet: the sandbox must run offline and deterministically in
CI. The `Retriever` interface is the seam for adding an embedding retriever later
as a third fused signal; the retrieval eval tells us whether it is worth it.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

import numpy as np
from rank_bm25 import BM25Okapi

from .chunker import Chunk, build_chunks

STOP = set("a an the of to in on for and or is are be can i my me it this that with do does if at by from you your "
           "we our what how when will would should could there their they them have has was were any".split())
# One-directional expansions: colloquial shopper wording -> wording used in the knowledge base.
# Expansions are appended once while the original query is repeated, so they can add recall
# without outvoting what the shopper actually said (e.g. "return" must not become "refund").
SYNONYMS = {
    "money back": ["refund"], "refnd": ["refund"], "reimburse": ["refund"],
    "send back": ["return"], "bring back": ["return"],
    "deadline": ["window"], "how many days": ["window"], "how long do i have": ["window"],
    "swap": ["exchange"], "bigger size": ["size exchange"], "smaller size": ["size exchange"],
    "broken": ["damaged"], "cracked": ["damaged"], "shattered": ["damaged"],
    "stopped working": ["defective warranty"], "faulty": ["defective warranty"], "doesn't work": ["defective warranty"],
    "rash": ["allergic reaction"], "allergy": ["allergic reaction"], "irritation": ["allergic reaction"],
    "lit": ["burned"], "used": ["opened"], "wore": ["worn"],
    "stolen": ["delivery protection"], "lost": ["lost in transit"], "porch": ["stolen after delivery"],
    "late": ["delays"], "not covered": ["not covered"], "insurance": ["protection"], "insure": ["protection"],
    "customs": ["duties taxes"], "stuck": ["held"], "christmas": ["peak season"], "holiday": ["peak season"],
    "slower": ["extra days"], "perfume": ["fragrances liquid cosmetics"], "fragrance": ["liquid cosmetics"],
    "serum": ["skincare"], "gas stove": ["gas appliances"], "camping stove": ["gas appliances"],
    "power bank": ["standalone lithium batteries"], "battery pack": ["standalone lithium batteries"],
    "tracking": ["track"], "address": ["delivery address"],
    "return policy": ["return window", "return shipping"], "returns policy": ["return window", "return shipping"],
}


def tokenize(text: str) -> list[str]:
    toks = re.findall(r"[a-z0-9]+", text.lower())
    out = []
    for t in toks:
        if t in STOP:
            continue
        if len(t) > 4 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        out.append(t)
    return out


def expand(query: str) -> str:
    from ..tools.kb import COUNTRY_ALIASES, country_name
    low = " " + re.sub(r"[^a-z0-9' ]", " ", query.lower()) + " "
    extra = [v for k, vs in SYNONYMS.items() if f" {k} " in low for v in vs]
    for alias, code in COUNTRY_ALIASES.items():
        if len(alias) > 2 and f" {alias} " in low or re.search(rf"\b{code}\b", query):
            extra.append(country_name(code))
    return f"{query} {query} " + " ".join(extra)


def char_ngrams(text: str, n: int = 4) -> Counter:
    t = " " + re.sub(r"\s+", " ", text.lower()) + " "
    return Counter(t[i:i + n] for i in range(len(t) - n + 1))


@dataclass
class Hit:
    chunk: Chunk
    score: float          # fused RRF score
    bm25: float
    char: float
    rank: int


class HybridRetriever:
    def __init__(self, chunks: list[Chunk] | None = None, rrf_k: int = 60):
        self.chunks = chunks or build_chunks()
        self.by_id = {c.id: c for c in self.chunks}
        self.rrf_k = rrf_k
        docs = [tokenize(f"{c.doc_title} {c.section} {c.section} {c.text} {c.meta.get('merchant', '')}") for c in self.chunks]
        self.bm25 = BM25Okapi(docs)
        grams = [char_ngrams(f"{c.section} {c.text}") for c in self.chunks]
        vocab = {g: i for i, g in enumerate(sorted({g for cg in grams for g in cg}))}
        df = np.zeros(len(vocab))
        for cg in grams:
            for g in cg:
                df[vocab[g]] += 1
        self.idf = np.log((1 + len(grams)) / (1 + df)) + 1
        self.vocab = vocab
        self.mat = np.vstack([self._vec(cg) for cg in grams])

    def _vec(self, cg: Counter) -> np.ndarray:
        v = np.zeros(len(self.vocab))
        for g, n in cg.items():
            i = self.vocab.get(g)
            if i is not None:
                v[i] = (1 + math.log(n)) * self.idf[i]
        norm = np.linalg.norm(v)
        return v / norm if norm else v

    def search(self, query: str, k: int = 4, filters: dict | None = None, min_bm25: float = 1.0) -> dict:
        q = expand(query)
        bm = self.bm25.get_scores(tokenize(q))
        ch = self.mat @ self._vec(char_ngrams(q))
        idx = list(range(len(self.chunks)))
        if filters:
            idx = [i for i in idx if _match(self.chunks[i], filters)]
        if not idx:
            return {"hits": [], "considered": 0, "filters": filters}
        bm_rank = {i: r for r, i in enumerate(sorted(idx, key=lambda i: -bm[i]))}
        ch_rank = {i: r for r, i in enumerate(sorted(idx, key=lambda i: -ch[i]))}
        fused = sorted(idx, key=lambda i: -(1 / (self.rrf_k + bm_rank[i]) + 1 / (self.rrf_k + ch_rank[i])))
        hits = []
        for r, i in enumerate(fused[:k]):
            if bm[i] < min_bm25 and ch[i] < 0.12:
                continue  # neither signal is confident: don't feed noise to the model
            hits.append(Hit(self.chunks[i], round(1 / (self.rrf_k + bm_rank[i]) + 1 / (self.rrf_k + ch_rank[i]), 5),
                            round(float(bm[i]), 3), round(float(ch[i]), 3), r + 1))
        return {"hits": hits, "considered": len(idx), "filters": filters}


def _match(c: Chunk, filters: dict) -> bool:
    for key, want in filters.items():
        have = c.meta.get(key)
        if isinstance(want, (list, tuple, set)):
            if have not in want:
                return False
        elif have != want:
            return False
    return True


_default: HybridRetriever | None = None


def default_retriever() -> HybridRetriever:
    global _default
    if _default is None:
        _default = HybridRetriever()
    return _default
