"""Budget-adaptive task routing, progressive skills and versioned evidence for SDLC v3.

A self-contained *simulator*; does not call an LLM, run cloud agents, or approve PRs.
Tokens and latency are declared synthetic accounting proxies, never real billing.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parent
TIERS = ("deterministic", "small-model", "frontier-agent")
COST = {
    "deterministic": {"input": 0, "output": 0, "latency_ms": 8},
    "small-model": {"input": 420, "output": 190, "latency_ms": 620},
    "frontier-agent": {"input": 3500, "output": 1250, "latency_ms": 4200},
}


def fingerprint(data: object) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Task:
    id: str
    request: str
    kind: Literal["lookup", "summary", "incident", "code_change", "review", "release"]
    tenant: str = "demo"
    issue_revision: int = 1
    commit_sha: str = "demo-commit-a1"
    risk: Literal["low", "normal", "high"] = "normal"
    required_skills: tuple[str, ...] = ()


@dataclass(frozen=True)
class Evidence:
    id: str
    tenant: str
    source: str
    source_revision: int
    kind: Literal["raw", "curated", "receipt"]
    text: str
    trusted: bool = False
    issue_id: str = ""
    issue_revision: int = 1
    commit_sha: str = ""
    ttl_run: int = 1_000_000
    tags: tuple[str, ...] = ()


class VersionedEvidenceStore:
    """Small, ephemeral VCG evidence store with mandatory tenant/version checks.

    Deliberately excludes arbitrary filesystem/network ingestion and raw secrets.
    The simulated run counter provides deterministic expiry for tests.
    """

    def __init__(self):
        self.records: dict[tuple[str, str], Evidence] = {}
        self.run_clock = 0
        self.invalidations = 0

    def add(self, item: Evidence):
        if not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,96}", item.tenant):
            raise ValueError("invalid tenant")
        if item.kind == "receipt" and (not item.issue_id or not item.commit_sha):
            raise ValueError("receipts must bind to issue and commit")
        if not 0 <= item.source_revision or not 0 <= item.ttl_run:
            raise ValueError("invalid revision or TTL")
        # Store a newer version; an older observation cannot overwrite the current fact.
        key = (item.tenant, item.id)
        prior = self.records.get(key)
        if prior and item.source_revision < prior.source_revision:
            raise ValueError("cannot overwrite a newer source revision")
        if prior and item.source_revision > prior.source_revision:
            self.invalidations += 1
        self.records[key] = item

    def get(self, tenant: str, id: str, *, revision: int | None = None,
            issue_id: str = "", commit_sha: str = "") -> Evidence | None:
        item = self.records.get((tenant, id))
        if item is None or self.run_clock > item.ttl_run:
            return None
        if revision is not None and revision != item.source_revision:
            return None
        if item.kind == "receipt" and (issue_id != item.issue_id or commit_sha != item.commit_sha):
            return None
        return item

    def find(self, task: Task) -> list[Evidence]:
        # Exact tenant and bound revision. In production add ACL filters per user as well.
        return [item for (tenant, _), item in self.records.items()
                if tenant == task.tenant and item.issue_id == task.id
                and item.issue_revision == task.issue_revision
                and item.commit_sha == task.commit_sha
                and item.kind != "receipt" and self.run_clock <= item.ttl_run]


@dataclass(frozen=True)
class SkillMetadata:
    name: str
    description: str
    path: str
    minimum_tier: str


class SkillCatalog:
    """Load metadata at startup and the instruction body only on explicit invocation."""
    def __init__(self, base: Path = ROOT / "skills"):
        self.skills: dict[str, SkillMetadata] = {}
        for file in sorted(base.glob("*/SKILL.md")):
            # A minimal, safe YAML-frontmatter subset, no user-provided expressions.
            source = file.read_text(encoding="utf-8")
            match = re.match(r"\A---\n(.*?)\n---\n", source, flags=re.S)
            if not match:
                continue
            metadata = {}
            for line in match.group(1).splitlines():
                key, sep, val = line.partition(":")
                if sep and key.strip() in {"name", "description", "minimum_tier"}:
                    metadata[key.strip()] = val.strip().strip('"')
            name = metadata.get("name", "")
            tier = metadata.get("minimum_tier", "")
            if name != file.parent.name or tier not in TIERS:
                raise ValueError("invalid skill manifest")
            self.skills[name] = SkillMetadata(name, metadata.get("description", ""),
                                              file.relative_to(base).as_posix(), tier)
        self.load_count = 0
        self.loaded_instruction_chars = 0
        self.base = base

    def metadata(self) -> list[dict]:
        return [dataclasses.asdict(x) for x in self.skills.values()]

    def invoke(self, name: str) -> str:
        spec = self.skills[name]
        body = (self.base / spec.path).read_text(encoding="utf-8").split("---\n", 2)[-1]
        self.load_count += 1
        self.loaded_instruction_chars += len(body)
        return body


class TierRouter:
    """Fail-closed, rule-based routing; cost budget never downgrades safety requirements."""
    def __init__(self, catalog: SkillCatalog):
        self.catalog = catalog

    def decide(self, task: Task, remaining_budget: int) -> dict:
        if task.kind == "lookup" and task.risk == "low":
            selected, reason = "deterministic", "Exact read-only lookup can use indexed evidence."
        elif task.kind in {"summary", "incident"} and task.risk != "high":
            selected, reason = "small-model", "Bounded evidence synthesis/investigation."
        else:
            selected, reason = "frontier-agent", "Code, independent review, release or high-risk work."
        for name in task.required_skills:
            if name not in self.catalog.skills:
                return {"tier": selected, "status": "unknown_skill", "reason": "Skill not registered.", "minimum_tokens": 0}
            minimum = self.catalog.skills[name].minimum_tier
            if TIERS.index(minimum) > TIERS.index(selected):
                selected = minimum
                reason = f"Skill {name} requires {minimum}."
        c = COST[selected]
        min_tokens = c["input"] + c["output"]
        if min_tokens > remaining_budget:
            return {"tier": selected, "status": "budget_blocked", "reason": "No safe downgrade to cheaper tier.", "minimum_tokens": min_tokens}
        return {"tier": selected, "status": "routed", "reason": reason, "minimum_tokens": min_tokens}


class TieredOperatingSystem:
    """Demonstration of skill matching, scoped evidence and revision-bound receipts."""

    def __init__(self, catalog: SkillCatalog | None = None):
        self.catalog = catalog or SkillCatalog()
        self.router = TierRouter(self.catalog)
        self.store = VersionedEvidenceStore()
        self.completed: dict[str, dict] = {}

    def run(self, tasks: list[Task], budget_tokens: int = 20_000) -> dict:
        if not isinstance(budget_tokens, int) or not 0 <= budget_tokens <= 1_000_000:
            raise ValueError("invalid synthetic budget")
        remaining = budget_tokens
        results = []
        seen = set()
        synthetic_cost = 0
        baseline_cost = 0
        total_latency_ms = 0
        deduplicated = 0
        approval_blocks = 0
        for task in tasks:
            dedup_key = fingerprint((task.tenant, task.kind, task.request, task.issue_revision, task.commit_sha))
            if dedup_key in seen:
                deduplicated += 1
                results.append({"id": task.id, "tier": "none", "status": "deduplicated", "tokens_modeled": 0})
                continue
            seen.add(dedup_key)
            # Naive comparator: all tasks call a frontier agent; identical task set.
            baseline_cost += COST["frontier-agent"]["input"] + COST["frontier-agent"]["output"]
            result = self.router.decide(task, remaining)
            if result["status"] != "routed":
                results.append({"id": task.id, **result, "tokens_modeled": 0})
                continue
            tier = result["tier"]
            evidence = self.store.find(task)
            # Missing evidence on any task must not be mistaken for verified progress.
            if not evidence:
                results.append({"id": task.id, "tier": tier, "status": "evidence_missing", "tokens_modeled": 0,
                                "reason": "No fresh, authorized evidence at this revision."})
                continue
            instructions = [self.catalog.invoke(name) for name in task.required_skills]
            # Cost proxy: fixed tier model + length of loaded skill text + context, not billed tokens.
            context_units = sum(len(x.text.split()) for x in evidence)
            skill_units = sum(len(x.split()) for x in instructions)
            overhead = context_units + skill_units
            # Local rules/index lookups have no LLM billable tokens.
            cost = 0 if tier == "deterministic" else result["minimum_tokens"] + overhead
            if cost > remaining:
                results.append({"id": task.id, "tier": tier, "status": "budget_blocked", "tokens_modeled": 0,
                                "reason": "Evidence/skill context would exceed remaining budget."})
                continue
            remaining -= cost
            synthetic_cost += cost
            total_latency_ms += COST[tier]["latency_ms"]
            # No actual actions: the receipt describes a *proposed* route/verification stage.
            gate = "human_approval_required" if task.risk == "high" or task.kind == "release" else "proposed_not_executed"
            if gate == "human_approval_required":
                approval_blocks += 1
            receipt = {"id": task.id, "tier": tier, "status": gate, "tokens_modeled": cost,
                       "evidence_ids": [e.id for e in evidence], "issue_revision": task.issue_revision,
                       "commit_sha": task.commit_sha, "skills": list(task.required_skills),
                       "context_fingerprint": fingerprint([task.issue_revision, task.commit_sha,
                                                           [[e.id, e.source_revision] for e in evidence]])}
            results.append(receipt)
        return {"metadata": {"simulated_workers": True, "provider_calls": 0, "git_writes": 0,
                              "tokens": "synthetic proxy; not measured model usage or billing"},
                "summary": {"tasks": len(tasks), "routed": sum(x["status"] in {"proposed_not_executed", "human_approval_required"} for x in results),
                            "tier_counts": {tier: sum(x.get("tier") == tier for x in results) for tier in TIERS},
                            "modeled_tokens": synthetic_cost, "naive_frontier_tokens": baseline_cost,
                            "modeled_token_reduction_pct": round(100 * (baseline_cost - synthetic_cost) / baseline_cost, 1) if baseline_cost else 0,
                            "modeled_latency_ms": total_latency_ms, "skill_instruction_loads": self.catalog.load_count,
                            "skill_instruction_chars": self.catalog.loaded_instruction_chars,
                            "deduplicated": deduplicated, "approval_blocks": approval_blocks,
                            "remaining_budget": remaining, "evidence_invalidations": self.store.invalidations},
                "results": results, "skills": self.catalog.metadata()}


def demonstration() -> dict:
    engine = TieredOperatingSystem()
    tasks = [
        Task("ENG-101", "Find revoked-token validator", "lookup", risk="low", required_skills=("context-refresh",)),
        Task("ENG-102", "Summarize current API trace regression", "summary", required_skills=("issue-triage",)),
        Task("ENG-103", "Investigate error-rate anomaly", "incident", required_skills=("issue-triage",)),
        Task("ENG-104", "Implement guarded token revocation", "code_change", risk="high", required_skills=("context-refresh",)),
        Task("ENG-105", "Independent security PR review", "review", required_skills=("independent-review",)),
        Task("ENG-106", "Approve a deployment", "release", risk="high", required_skills=("release-readiness",)),
        Task("ENG-107", "Summarize current API trace regression", "summary", required_skills=("issue-triage",)),
    ]
    # Use actual v2 BM25 + sparse TF-IDF + graph traversal for the demonstration.
    # Corpora are fully fabricated public fixture files and sample issue data.
    from retrieval import ContextRetriever, fixture_corpus
    corpus, _ = fixture_corpus()
    retriever = ContextRetriever(corpus)
    chosen = {}
    for task in tasks:
        if task.kind in {"lookup", "summary", "incident"}:
            query = {"ENG-101": "Which function validates revoked access tokens?",
                     "ENG-102": "What does issue ENG-102 request?",
                     "ENG-103": "What issue is linked to observed 401 failure and which component is affected?",
                     "ENG-107": "What does issue ENG-102 request?"}.get(task.id, task.request)
            result = retriever.retrieve(query, mode="adaptive", tenant=task.tenant, budget_words=130)
            chosen[task.id] = result
            for found in result["results"]:
                source = retriever.docs[found["id"]]
                engine.store.add(Evidence(f"{task.id}:{source.id}", task.tenant, source.id,
                                          source.revision, "curated", source.payload,
                                          issue_id=task.id, issue_revision=task.issue_revision,
                                          commit_sha=task.commit_sha, trusted=False))
        else:
            # Independent synthetic task-scoped evidence; not a real CI/PR receipt.
            engine.store.add(Evidence(f"fixture:{task.id}", task.tenant, f"example/{task.id}", 1,
                                      "curated", "Synthetic task specification and work scope; not verified code.",
                                      issue_id=task.id, issue_revision=task.issue_revision,
                                      commit_sha=task.commit_sha, trusted=False))
    report = engine.run(tasks)
    report["retrieval"] = {"index_version": retriever.version_signature,
                           "mode": "adaptive", "retrieval_calls": len(chosen),
                           "context_words_proxy": sum(v["context_words_proxy"] for v in chosen.values()),
                           "strategy_per_task": {k: v["strategy"] for k, v in chosen.items()},
                           "note": "Actual local sparse BM25/TF-IDF retrieval; no embeddings, neural models or hosted LLM."}
    return report


if __name__ == "__main__":
    print(json.dumps(demonstration(), indent=2))