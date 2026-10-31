"""Autonomous SDLC research PoC.

The workers are deterministic SIMULATORS, not LLMs. Token counts are modeled,
not provider usage. The repository indexing and unit test gate are executable.
No code is written to the fixture repository and nothing is merged/deployed.
"""
from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
import os
import re
import resource
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SAMPLE_REPO = ROOT / "fixture_repo"
OUT = ROOT / "output"
IGNORED = {".git", ".venv", "node_modules", "__pycache__", ".next", "dist", "build"}
EXTENSIONS = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".java", ".md"}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:14]


@dataclass
class Issue:
    id: str
    title: str
    paths: list[str]
    intent: str
    risk: str = "normal"
    depends_on: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    revision: int = 1
    origin: str = "Linear-like issue"
    severity: str = "normal"


DEMO_ISSUES = [
    Issue("ENG-101", "Guard revoked tokens against reuse", ["src/auth.py", "tests/test_app.py"],
          "auth-revocation", "high", [], ["Regression test passes", "Independent reviews", "Security signoff", "Human approval"]),
    Issue("ENG-102", "Include trace IDs on rejected API requests", ["src/api.py", "tests/test_app.py"],
          "request-tracing", "normal", [], ["Contract test passes", "Independent reviews"]),
    Issue("ENG-103", "Clarify onboarding documentation", ["README.md"], "onboarding-docs", "low", [],
          ["Documentation change reviewed", "Independent reviews"]),
    Issue("ENG-104", "Investigate elevated 401 counts", ["src/auth.py", "src/api.py"],
          "auth-observability", "normal", ["ENG-101"], ["Incident correlated to code", "Independent reviews"] ,
          origin="Sanitized telemetry signal", severity="warning"),
    Issue("ENG-105", "Duplicate: clarify onboarding documentation", ["README.md"], "onboarding-docs", "low", [],
          ["Documentation change reviewed", "Independent reviews"], origin="Duplicate issue"),
]


class VersionedContextGraph:
    """Minimal revisioned graph: files, Python symbols/imports, issues and signals.

    This is NOT a full semantically accurate cross-language knowledge graph.
    Source code is indexed as metadata, never sent to an external system.
    """
    def __init__(self, repo: Path):
        self.repo = repo.resolve()
        self.files: dict[str, dict] = {}
        self.nodes: list[dict] = []
        self.edges: list[dict] = []
        self._index()

    def _index(self):
        paths: list[Path] = []
        for root, dirs, files in os.walk(self.repo, followlinks=False):
            dirs[:] = [d for d in dirs if d not in IGNORED and not (Path(root) / d).is_symlink()]
            for name in files:
                p = Path(root) / name
                if p.is_symlink() or p.suffix not in EXTENSIONS or p.stat().st_size > 256_000:
                    continue
                paths.append(p)
                if len(paths) >= 2000:
                    break
            if len(paths) >= 2000:
                break
        for p in sorted(paths):
            rel = p.relative_to(self.repo).as_posix()
            data = p.read_bytes()
            item = {"path": rel, "sha": sha(data), "bytes": len(data), "language": p.suffix.lstrip('.'), "symbols": [], "imports": []}
            content = data.decode("utf-8", errors="replace")
            if p.suffix == ".py":
                try:
                    tree = ast.parse(content)
                    for node in ast.walk(tree):
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                            item["symbols"].append(node.name)
                        if isinstance(node, ast.Import):
                            item["imports"].extend(n.name for n in node.names)
                        if isinstance(node, ast.ImportFrom) and node.module:
                            item["imports"].append(node.module)
                except SyntaxError:
                    pass
            elif p.suffix in {".ts", ".tsx", ".js", ".jsx", ".go", ".java"}:
                # Discovery heuristic only. A production version uses tree-sitter/LSP.
                item["symbols"] = list(dict.fromkeys(re.findall(r"(?:function|class|func|interface)\s+(\w+)", content)))[:70]
            self.files[rel] = item
            self.nodes.append({"id": f"file:{rel}", "type": "file", "label": rel, "hash": item["sha"]})
            for symbol in item["symbols"][:50]:
                sid = f"symbol:{rel}:{symbol}"
                self.nodes.append({"id": sid, "type": "symbol", "label": symbol})
                self.edges.append({"from": f"file:{rel}", "to": sid, "type": "defines"})
        for rel, item in self.files.items():
            for imp in item["imports"]:
                target = imp.replace(".", "/") + ".py"
                if target in self.files:
                    self.edges.append({"from": f"file:{rel}", "to": f"file:{target}", "type": "imports"})

    def with_issues(self, issues: list[Issue]):
        nodes = list(self.nodes)
        edges = list(self.edges)
        for issue in issues:
            nodes.append({"id": f"issue:{issue.id}", "type": "issue", "label": issue.title, "revision": issue.revision})
            for path in issue.paths:
                if path in self.files:
                    edges.append({"from": f"issue:{issue.id}", "to": f"file:{path}", "type": "touches"})
            for dep in issue.depends_on:
                edges.append({"from": f"issue:{issue.id}", "to": f"issue:{dep}", "type": "depends_on"})
            if issue.origin == "Sanitized telemetry signal":
                signal_id = f"signal:{issue.id}"
                nodes.append({"id": signal_id, "type": "signal", "label": "401 rate anomaly (synthetic)"})
                edges.append({"from": signal_id, "to": f"issue:{issue.id}", "type": "opened"})
        return {"nodes": nodes, "edges": edges}

    def relevant_files(self, issue: Issue) -> list[str]:
        result = set(path for path in issue.paths if path in self.files)
        # One-hop dependencies only; intentional small context retrieval.
        for path in tuple(result):
            for imp in self.files[path]["imports"]:
                target = imp.replace(".", "/") + ".py"
                if target in self.files:
                    result.add(target)
        return sorted(result)

    def context(self, issue: Issue, compact: bool, revision_override: int | None = None) -> dict:
        selected = self.relevant_files(issue) if compact else sorted(self.files)
        rev = revision_override if revision_override is not None else issue.revision
        details = [{"path": p, "sha": self.files[p]["sha"], "symbols": self.files[p]["symbols"][:20]}
                   for p in selected]
        packet = {"issue": issue.id, "issue_revision": rev, "paths": details, "acceptance": issue.acceptance,
                  "dependencies": issue.depends_on}
        signature = sha(json.dumps(packet, sort_keys=True).encode())
        # A conservative approximation of serialized context transport, not model tokens.
        return {"signature": signature, "issue_revision": rev, "files": selected,
                "bytes": len(json.dumps(packet)), "packet": packet}


class Harness:
    def __init__(self, repo: Path = SAMPLE_REPO, extra_issues: list[Issue] | None = None):
        self.repo = Path(repo).resolve()
        self.context_graph = VersionedContextGraph(self.repo)
        self.issues = [dataclasses.replace(i, paths=i.paths[:], depends_on=i.depends_on[:],
                                          acceptance=i.acceptance[:]) for i in (DEMO_ISSUES + (extra_issues or []))]

    def test_gate(self) -> dict:
        """Actually invoke unittest, read-only against fixture code."""
        result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                                cwd=self.repo, capture_output=True, text=True, timeout=15, check=False)
        return {"passed": result.returncode == 0, "exit_code": result.returncode,
                "summary": (result.stdout + result.stderr)[-1300:]}

    def run(self, mode="optimized", budget=120000, max_parallel=3, inject_change=False) -> dict:
        assert mode in {"baseline", "optimized"}
        budget = max(0, int(budget))
        max_parallel = max(1, min(10, int(max_parallel)))
        optimized = mode == "optimized"
        # Execute the real test gate; not a synthetic QA assertion.
        test = self.test_gate()
        events: list[dict] = []
        traces: list[dict] = []
        results: list[dict] = []
        lock_waves: list[list[str]] = []
        event_counter = 0
        time_base = "2026-10-31T17:30:00Z"
        tokens = 0
        deduped = 0
        stale = 0
        cache_hits = 0
        approval_blocks = 0
        contexts: dict[str, dict] = {}
        completed_intents: set[str] = set()
        verified_ids: set[str] = set()
        processed_ids: set[str] = set()
        task_index = {i.id: i for i in self.issues}
        test_runs = 0
        total_context_bytes = 0
        # Pseudo workload concurrency: waves with no conflicting write footprints.
        queue = list(self.issues)

        def log(kind, issue_id, message, **extra):
            nonlocal event_counter
            event_counter += 1
            events.append({"sequence": event_counter, "at": time_base, "type": kind,
                           "issue": issue_id, "message": message, **extra})

        log("feed", "ENG-104", "Sanitized observability signal opened issue ENG-104: elevated 401 ratio")
        # Schedule dependency-aware waves, using write-set conflicts.
        pending = list(queue)
        while pending:
            batch: list[Issue] = []
            write_set: set[str] = set()
            for issue in pending:
                if issue.depends_on and not all(dep in processed_ids for dep in issue.depends_on):
                    continue
                conflict = bool(write_set.intersection(issue.paths))
                if conflict and optimized:
                    continue
                if len(batch) >= max_parallel:
                    break
                batch.append(issue)
                write_set.update(issue.paths)
            if not batch:  # Defensive dependency handling
                break
            lock_waves.append([i.id for i in batch])
            log("schedule", "-", f"Wave {len(lock_waves)}: " + ", ".join(i.id for i in batch))
            for issue in batch:
                pending.remove(issue)
                processed_ids.add(issue.id)
                if optimized and issue.intent in completed_intents:
                    deduped += 1
                    results.append({"id": issue.id, "status": "deduplicated", "title": issue.title, "tokens": 0,
                                    "dod_passed": False, "files": issue.paths, "reviewers": [], "stale": False})
                    log("dedup", issue.id, "Equivalent intent already processed; duplicate agent work suppressed")
                    continue
                # For the PoC, these stages are mocked, with a REAL unittest gate below.
                stages = ["planner", "implementer", "reviewer_a", "reviewer_b", "qa"]
                if issue.risk == "high" or not optimized:
                    stages.append("security")
                if not optimized:
                    stages.append("compliance")
                if optimized and inject_change and issue.id == "ENG-102":
                    # A changed DoD must trigger replanning + rework, not merely a fresh review.
                    stages[2:2] = ["planner_rebase", "implementer_rebase"]
                initial_context = self.context_graph.context(issue, compact=optimized)
                # A deterministic test change is injected after implementation.
                changed_revision = issue.revision + int(inject_change and issue.id == "ENG-102")
                cost_est = 0
                stage_contexts: list[tuple[str, dict, int, int]] = []
                for stage in stages:
                    # Baseline retains its initial context even after a new issue revision.
                    current_revision = changed_revision if optimized and stage not in {"planner", "implementer"} else issue.revision
                    ctx = self.context_graph.context(issue, compact=optimized, revision_override=current_revision) if optimized else initial_context
                    signature = ctx["signature"]
                    if optimized and signature in contexts:
                        context_load = 60
                    else:
                        # Modeled prompt sizing; baseline repeatedly consumes whole repository summaries.
                        context_load = 450 + (ctx["bytes"] // 3) + (1200 if not optimized else 0)
                    output = {"planner": 420, "implementer": 880, "reviewer_a": 325,
                              "reviewer_b": 325, "qa": 240, "security": 330, "compliance": 230,
                              "planner_rebase": 310, "implementer_rebase": 570}[stage]
                    stage_contexts.append((stage, ctx, context_load, output))
                    cost_est += context_load + output
                if tokens + cost_est > budget:
                    log("budget", issue.id, f"Admission denied: projected {cost_est} modeled tokens exceeds remaining {budget-tokens}")
                    results.append({"id": issue.id, "status": "budget_blocked", "title": issue.title,
                                    "tokens": 0, "dod_passed": False, "files": issue.paths, "reviewers": [], "stale": False})
                    continue
                start_tokens = tokens
                review_ids = []
                stale_here = False
                for stage, ctx, input_tokens, output_tokens in stage_contexts:
                    context_signature = ctx["signature"]
                    was_cache_hit = bool(optimized and context_signature in contexts)
                    if was_cache_hit:
                        cache_hits += 1
                    else:
                        contexts[context_signature] = ctx
                        total_context_bytes += ctx["bytes"]
                    if inject_change and issue.id == "ENG-102" and stage == ("planner_rebase" if optimized else "reviewer_a"):
                        log("invalidation", issue.id, "Issue acceptance criteria changed from revision 1 to 2")
                    if stage == "implementer_rebase":
                        log("rework", issue.id, "Replanned implementation against revision 2 before independent review (simulated)")
                    is_stale = ctx["issue_revision"] != changed_revision and stage not in {"planner", "implementer"}
                    if is_stale:
                        stale += 1
                        stale_here = True
                        log("stale_context", issue.id, f"{stage} has revision {ctx['issue_revision']}, current revision is {changed_revision}")
                    tokens += input_tokens + output_tokens
                    actor_id = {"reviewer_a": "review-agent-A", "reviewer_b": "review-agent-B"}.get(stage, f"{stage}-agent")
                    if stage.startswith("reviewer"):
                        review_ids.append(actor_id)
                    if stage == "qa":
                        test_runs += 1 if not optimized or test_runs == 0 else 0
                    traces.append({"issue": issue.id, "stage": stage, "agent": actor_id, "context_id": context_signature,
                                   "input_tokens_modeled": input_tokens, "output_tokens_modeled": output_tokens,
                                   "cached_context": was_cache_hit,
                                   "context_bytes": ctx["bytes"], "context_revision": ctx["issue_revision"],
                                   "stale": is_stale, "duration_ms_modeled": 150 + output_tokens // 2})
                    log("agent", issue.id, f"{actor_id} completed {stage} (simulated)",
                        input_tokens=input_tokens, output_tokens=output_tokens, context=context_signature)
                independent = len(set(review_ids)) >= 2 and "implementer-agent" not in review_ids
                verified = bool(test["passed"] and independent and not stale_here)
                if issue.risk == "high" and verified:
                    approval_blocks += 1
                    status = "human_approval_required"
                    log("approval", issue.id, "Independent checks pass; merge/deploy deliberately blocked pending human approval")
                elif verified:
                    status = "simulation_gates_passed"
                    verified_ids.add(issue.id)
                    log("verification", issue.id, "Simulated review evidence + actual fixture unittest passed; no PR was merged")
                else:
                    status = "stale_or_failed_gate"
                    log("gate_fail", issue.id, "Review/test/freshness gate failed; no merge allowed")
                completed_intents.add(issue.intent)
                results.append({"id": issue.id, "status": status, "title": issue.title,
                                "tokens": tokens - start_tokens, "dod_passed": verified,
                                "files": issue.paths, "reviewers": review_ids, "stale": stale_here,
                                "human_gate": issue.risk == "high"})

        unique_intents = len({i.intent for i in self.issues})
        done_intents = len({task_index[r["id"]].intent for r in results if r["dod_passed"]})
        metadata = {"mode": mode, "token_source": "synthetic modeled tokens; NOT actual provider usage",
                    "simulated_workers": True, "actual_prs": 0, "actual_deployments": 0,
                    "real_fixture_tests": test["passed"], "injected_change": inject_change,
                    "source_repo": str(self.repo)}
        return {
            "metadata": metadata,
            "summary": {"tokens_modeled": tokens, "budget_tokens_modeled": budget,
                        "unique_intents": unique_intents, "dod_verified_simulated": done_intents,
                        "deduplicated_issues": deduped, "stale_context_uses": stale,
                        "context_cache_hits": cache_hits, "unique_contexts": len(contexts),
                        "context_bytes_loaded": total_context_bytes, "human_approval_blocks": approval_blocks,
                        "wave_count": len(lock_waves), "max_parallel": max_parallel,
                        "qa_test_rechecks_modeled": test_runs, "real_fixture_test_invocations": 1,
                        "local_python_max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss},
            "issues": results, "waves": lock_waves, "traces": traces, "events": events,
            "graph": self.context_graph.with_issues(self.issues), "test_result": test,
        }


def build_demo(budget=120000, max_parallel=3, inject_change=False, repo=SAMPLE_REPO, dest=OUT, extra_issues=None):
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    harness = Harness(Path(repo), extra_issues=extra_issues)
    baseline = harness.run("baseline", budget=budget, max_parallel=max_parallel, inject_change=inject_change)
    optimized = harness.run("optimized", budget=budget, max_parallel=max_parallel, inject_change=inject_change)
    for name, value in (("baseline", baseline), ("optimized", optimized)):
        (dest / f"{name}.json").write_text(json.dumps(value, indent=2), encoding="utf-8")
    b = baseline["summary"]["tokens_modeled"]
    o = optimized["summary"]["tokens_modeled"]
    compare = {"baseline_tokens": b, "optimized_tokens": o, "modeled_token_reduction_pct": round((b-o)/b*100, 1) if b else None,
               "notes": "Illustrative model only, not measured LLM token savings. Quality is simulated except actual fixture unit tests."}
    (dest / "comparison.json").write_text(json.dumps(compare, indent=2), encoding="utf-8")
    return {"baseline": baseline, "optimized": optimized, "comparison": compare}


if __name__ == "__main__":
    print(json.dumps(build_demo()["comparison"], indent=2))
