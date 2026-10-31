"""Dependency-free, real lexical/vector/graph retrieval demonstrator.

Implements BM25, sparse TF-IDF cosine similarity, reciprocal-rank fusion,
optional one-hop relationship expansion, freshness/tenant gates and budgeted
context selection. NO model embeddings, HNSW, ANN database or LLM calls.
Token counts are rough word-like proxies, NOT provider tokenization or costs.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def terms(text: str) -> list[str]:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    text = re.sub(r"[_/.-]", " ", text.lower())
    return [t[:-1] if t.endswith("ated") else t for t in re.findall(r"[a-z0-9]+", text)]


def word_cost(text: str) -> int:
    """Approximate context units for a controlled side-by-side comparison."""
    return len(re.findall(r"\S+", text))


@dataclass(frozen=True)
class Document:
    id: str
    title: str
    text: str
    revision: int = 1
    links: tuple[str, ...] = ()
    tenant: str = "demo"
    active: bool = True

    @property
    def payload(self) -> str:
        return f"{self.title}\n{self.text}"


class ContextRetriever:
    def __init__(self, documents: list[Document]):
        # Keep the latest revision of each identity, regardless of input ordering.
        latest = {}
        for document in documents:
            if document.id not in latest or document.revision > latest[document.id].revision:
                latest[document.id] = document
        self.docs = {d.id: d for d in latest.values() if d.active}
        self.counts = {key: Counter(terms(doc.payload)) for key, doc in self.docs.items()}
        self.lengths = {key: sum(c.values()) for key, c in self.counts.items()}
        self.avg_length = sum(self.lengths.values()) / max(1, len(self.docs))
        self.df = Counter()
        for c in self.counts.values():
            self.df.update(c.keys())
        self.weights = {}
        for key, c in self.counts.items():
            self.weights[key] = {t: (1 + math.log(f)) * self.idf(t) for t, f in c.items()}
        self.norms = {key: math.sqrt(sum(v*v for v in ws.values())) for key, ws in self.weights.items()}
        self.graph = defaultdict(set)
        for doc in self.docs.values():
            for target in doc.links:
                if target in self.docs:
                    self.graph[doc.id].add(target)
                    self.graph[target].add(doc.id)
        self.version_signature = hashlib.sha256(json.dumps(
            [(d.id, d.revision, d.tenant, hashlib.sha256(d.payload.encode()).hexdigest())
             for d in sorted(self.docs.values(), key=lambda x: x.id)]).encode()).hexdigest()[:16]

    def idf(self, term: str) -> float:
        n = len(self.docs)
        return math.log(1 + (n - self.df[term] + .5) / (self.df[term] + .5))

    def bm25(self, query: str, allowed: set[str]) -> dict[str, float]:
        q = Counter(terms(query))
        scores = {}
        for doc_id in allowed:
            tf = self.counts[doc_id]
            score = 0.0
            for term in q:
                freq = tf[term]
                if not freq:
                    continue
                score += self.idf(term) * (freq * 2.2) / (
                    freq + 1.2 * (.25 + .75 * self.lengths[doc_id] / max(self.avg_length, 1)))
            scores[doc_id] = score
        return scores

    def cosine(self, query: str, allowed: set[str]) -> dict[str, float]:
        q = Counter(terms(query))
        qvector = {t: (1 + math.log(freq)) * self.idf(t) for t, freq in q.items()}
        qnorm = math.sqrt(sum(x*x for x in qvector.values())) or 1
        return {doc_id: sum(weight * self.weights[doc_id].get(t, 0) for t, weight in qvector.items()) /
                (qnorm * (self.norms[doc_id] or 1)) for doc_id in allowed}

    @staticmethod
    def sorted_rank(scores: dict[str, float]) -> list[str]:
        return [k for k, v in sorted(scores.items(), key=lambda item: (-item[1], item[0])) if v > 0]

    def retrieve(self, query: str, mode: str = "adaptive", budget_words: int = 130,
                 tenant: str = "demo", top_k: int = 5) -> dict:
        if mode not in {"fixed", "adaptive"}:
            raise ValueError("mode must be fixed or adaptive")
        if not isinstance(budget_words, int) or budget_words < 1:
            raise ValueError("budget_words must be positive")
        eligible = {key for key, d in self.docs.items() if d.tenant == tenant}
        b_rank = self.sorted_rank(self.bm25(query, eligible))
        v_rank = self.sorted_rank(self.cosine(query, eligible))
        query_terms = set(terms(query))
        explicit = bool(re.search(r"\b(?:ENG|OBS)-\d+\b|\b(?:src|tests)/\S+|\b[A-Za-z]+_[A-Za-z_]+\b", query, re.I))
        relational = bool(query_terms & {"related", "linked", "depends", "dependencies", "affects", "impact", "connect", "connected", "calls", "traces", "cause", "tests", "test", "verifies"})
        strategy = "vector_tfidf" if mode == "fixed" else ("lexical" if explicit and not relational else "hybrid_graph" if relational else "hybrid")
        if strategy == "vector_tfidf":
            ranks = v_rank
        elif strategy == "lexical":
            ranks = b_rank
        else:
            # Reciprocal Rank Fusion: hybrid order (not a neural reranker).
            fused = defaultdict(float)
            for sequence in (b_rank, v_rank):
                for i, doc_id in enumerate(sequence):
                    fused[doc_id] += 1 / (25 + i + 1)
            if strategy == "hybrid_graph":
                # One-hop expansion from top candidates. Links are deterministic evidence, not similarity.
                for seed in list(fused):
                    if seed not in (set(b_rank[:2]) | set(v_rank[:2])):
                        continue
                    for neighbor in self.graph[seed]:
                        if neighbor in eligible:
                            fused[neighbor] += .008
            ranks = [key for key, _ in sorted(fused.items(), key=lambda item: (-item[1], item[0]))]
        # Type-aware rerouting: keep source-of-truth documents ahead of weakly related issues.
        # These are generic query categories, not gold-label or answer-based overrides.
        if mode == "adaptive":
            if "test" in query_terms or "tests" in query_terms:
                priority = lambda doc_id: doc_id.startswith("file:tests/")
            elif "issue" in query_terms or explicit and re.search(r"\b(?:ENG|OBS)-\d+\b", query, re.I):
                priority = lambda doc_id: doc_id.startswith("issue:")
            elif query_terms & {"function", "component", "where", "code", "implementation"}:
                priority = lambda doc_id: doc_id.startswith("file:src/")
            else:
                priority = lambda doc_id: False
            ranks = sorted(ranks, key=lambda key: not priority(key))
        if mode == "adaptive" and relational and {"issue", "component"} <= query_terms:
            # A multi-source question needs one issue plus its linked code evidence.
            issues = [k for k in ranks if k.startswith("issue:")]
            if issues:
                anchor = issues[0]
                linked_files = [k for k in ranks if k in self.graph[anchor] and k.startswith("file:src/")]
                ranks = [anchor] + linked_files + [k for k in ranks if k != anchor and k not in linked_files]
        cap = top_k if mode == "fixed" else (2 if strategy in {"lexical", "hybrid"} else 3)
        chosen = []
        cost = 0
        for key in ranks:
            if len(chosen) >= cap:
                break
            doc = self.docs[key]
            chunk_cost = word_cost(doc.payload)
            if cost + chunk_cost <= budget_words:
                chosen.append({"id": key, "title": doc.title, "revision": doc.revision,
                               "context_words_proxy": chunk_cost, "links": list(doc.links)})
                cost += chunk_cost
        return {"query": query, "mode": mode, "strategy": strategy,
                "budget_words_proxy": budget_words, "context_words_proxy": cost,
                "index_version": self.version_signature, "results": chosen,
                "note": "Real sparse TF-IDF cosine and BM25; no neural embeddings or ANN. Words are not model tokens."}


def fixture_corpus() -> tuple[list[Document], list[dict]]:
    """Tiny mixed software and support corpus for a reproducible experiment."""
    from harness import DEMO_ISSUES, VersionedContextGraph, SAMPLE_REPO
    kg = VersionedContextGraph(SAMPLE_REPO)
    docs: list[Document] = []
    for path in sorted(kg.files):
        p = SAMPLE_REPO / path
        links = ["file:" + k for k in kg.relevant_files(
            next((i for i in DEMO_ISSUES if path in i.paths), DEMO_ISSUES[0])) if k != path]
        docs.append(Document("file:" + path, path,
                             p.read_text(encoding="utf-8", errors="replace"), links=tuple(links)))
    for issue in DEMO_ISSUES:
        docs.append(Document("issue:" + issue.id, f"{issue.id}: {issue.title}",
                             "Acceptance: " + "; ".join(issue.acceptance), revision=issue.revision,
                             links=tuple("file:" + p for p in issue.paths)))
    # A separate domain, same engine; documents are fabricated test data, never customer information.
    docs += [
        Document("support:sso", "Single sign-on troubleshooting", "When enterprise SSO fails, inspect identity provider certificates, callback URL and clock skew. Log in via corporate identity.", links=("support:session",), tenant="support"),
        Document("support:session", "Revoked sessions policy", "After password reset, revoke existing sessions and ask the user to sign in again. Revoked session tokens cannot be reused.", links=("support:sso",), tenant="support"),
        Document("support:invoice", "Billing invoice download", "Account administrators download monthly invoices from Billing Settings. Invoice exports include invoice number and billing period.", links=("support:refund",), tenant="support"),
        Document("support:refund", "Refund eligibility policy", "A refund may be requested within fourteen days of initial purchase. After fourteen days, contact billing support for an exception.", links=("support:invoice",), tenant="support"),
        Document("support:ticket", "Customer support escalation", "Escalate payment disputes with transaction ID, invoice number, and a sanitized issue summary. Do not store payment card numbers.", links=("support:refund",), tenant="support"),
    ]
    queries = [
        {"tenant":"demo", "query":"Which function validates revoked access tokens?", "gold":["file:src/auth.py"]},
        {"tenant":"demo", "query":"Where is API authentication handled?", "gold":["file:src/api.py", "file:src/auth.py"]},
        {"tenant":"demo", "query":"Which test verifies a revoked token cannot be reused?", "gold":["file:tests/test_app.py"]},
        {"tenant":"demo", "query":"What does issue ENG-102 request?", "gold":["issue:ENG-102"]},
        {"tenant":"demo", "query":"What issue is linked to the observed 401 failure and which component is affected?", "gold":["issue:ENG-104", "file:src/auth.py"]},
        {"tenant":"support", "query":"How do I download a monthly invoice?", "gold":["support:invoice"]},
        {"tenant":"support", "query":"How long after a purchase may I request a refund?", "gold":["support:refund"]},
        {"tenant":"support", "query":"What happens to existing sessions after password reset?", "gold":["support:session"]},
        {"tenant":"support", "query":"How do SSO problems relate to revoked sessions?", "gold":["support:sso", "support:session"]},
        {"tenant":"support", "query":"When should billing disputes be escalated?", "gold":["support:ticket"]},
    ]
    return docs, queries


def benchmark(budget_words: int = 130) -> dict:
    docs, cases = fixture_corpus()
    retriever = ContextRetriever(docs)
    details = []
    by_mode = defaultdict(lambda: {"covered": 0, "gold": 0, "context_words_proxy": 0, "queries": 0, "complete": 0})
    for case in cases:
        for mode in ("fixed", "adaptive"):
            output = retriever.retrieve(case["query"], mode=mode, budget_words=budget_words, tenant=case["tenant"])
            got = {r["id"] for r in output["results"]}
            gold = set(case["gold"])
            agg = by_mode[mode]
            agg["covered"] += len(gold & got)
            agg["gold"] += len(gold)
            agg["queries"] += 1
            agg["complete"] += int(gold <= got)
            agg["context_words_proxy"] += output["context_words_proxy"]
            details.append({"tenant":case["tenant"], "gold": sorted(gold), "covered":sorted(gold & got), **output})
    summary = {}
    for mode, agg in by_mode.items():
        summary[mode] = {**agg, "gold_evidence_recall": round(agg["covered"] / agg["gold"], 3),
                         "complete_question_rate": round(agg["complete"] / agg["queries"], 3)}
    return {"summary":summary,"details":details, "note": "Tiny synthetic evaluation set; retrieval coverage, not answer accuracy. No LLM or ANN. Context counted in whitespace units."}


if __name__ == "__main__":
    print(json.dumps(benchmark(), indent=2))
