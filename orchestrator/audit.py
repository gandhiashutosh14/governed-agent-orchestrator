"""
Audit: run a list of objectives end to end and report how the planner cascade
and the runtime behaved. The numbers a reviewer should look at are: how many
plans were valid on the first planner, how many runs answered, how many step
fallbacks fired, and how many approval gates were raised (and, in auto-approve
mode, resolved).
"""
from __future__ import annotations

import asyncio
import json
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .graph import Orchestrator


@dataclass
class AuditCase:
    objective: str
    status: str
    plan_source: str
    steps: int
    planner_fallback: bool
    step_fallbacks: int
    approvals_raised: int
    answered: bool
    ms: int
    answer: str = ""
    errors: List[str] = field(default_factory=list)


@dataclass
class AuditReport:
    llm: str
    cases: List[AuditCase]
    elapsed_s: float
    auto_approve: bool

    def summary(self) -> Dict[str, Any]:
        n = len(self.cases)
        return {
            "llm": self.llm,
            "objectives": n,
            "valid_plan_first_planner": sum(1 for c in self.cases if not c.planner_fallback and c.status != "failed"),
            "planner_fallbacks": sum(1 for c in self.cases if c.planner_fallback),
            "answered": sum(1 for c in self.cases if c.answered),
            "step_fallbacks": sum(c.step_fallbacks for c in self.cases),
            "approvals_raised": sum(c.approvals_raised for c in self.cases),
            "median_ms": int(statistics.median(c.ms for c in self.cases)) if n else 0,
            "elapsed_s": round(self.elapsed_s, 1),
        }

    def to_markdown(self) -> str:
        s = self.summary()
        lines = [f"# Orchestrator audit: {self.llm}", "",
                 f"{s['objectives']} objectives, auto-approve={'on' if self.auto_approve else 'off'}, {s['elapsed_s']} s.", "",
                 "| Metric | Value |", "|---|---|",
                 f"| Valid plan from the first planner | {s['valid_plan_first_planner']} / {s['objectives']} |",
                 f"| Planner fallbacks (heuristic used) | {s['planner_fallbacks']} |",
                 f"| Answered | {s['answered']} / {s['objectives']} |",
                 f"| Step fallbacks fired | {s['step_fallbacks']} |",
                 f"| Approval gates raised | {s['approvals_raised']} |",
                 f"| Median latency | {s['median_ms']} ms |", "",
                 "| # | Objective | Plan source | Steps | Status | Approvals | ms |", "|---|---|---|---|---|---|---|"]
        for i, c in enumerate(self.cases, 1):
            lines.append(f"| {i} | {c.objective} | {c.plan_source} | {c.steps} | {c.status} | {c.approvals_raised} | {c.ms} |")
        return "\n".join(lines)


async def run_audit(orch: Orchestrator, objectives: Sequence[str], *, auto_approve: bool = True) -> AuditReport:
    t0 = time.perf_counter()
    cases: List[AuditCase] = []
    for objective in objectives:
        t1 = time.perf_counter()
        snap = await orch.start(objective)
        approvals = 0
        while snap["status"] == "awaiting_approval":
            approvals += 1
            if not auto_approve:
                break
            snap = await orch.resume(snap["run_id"], True, by="audit")
        trace = orch.trace_for(snap["run_id"])
        planner_fb = bool(trace.of_type("planner_fallback"))
        step_fb = len(trace.of_type("step_fallback"))
        errors = [e for ev in trace.of_type("plan_rejected") for e in ev.data.get("errors", [])]
        plan = snap.get("plan") or {}
        cases.append(AuditCase(objective=objective, status=snap["status"], plan_source=snap.get("plan_source") or "-",
                               steps=len(plan.get("steps", [])), planner_fallback=planner_fb, step_fallbacks=step_fb,
                               approvals_raised=approvals, answered=snap["status"] == "completed",
                               ms=int((time.perf_counter() - t1) * 1000), answer=snap.get("answer", "")[:300], errors=errors[:3]))
    llm_name = "-"
    for p in getattr(orch.planner, "planners", []):
        if hasattr(p, "llm"):
            llm_name = getattr(p.llm, "name", "?")
    return AuditReport(llm=llm_name, cases=cases, elapsed_s=time.perf_counter() - t0, auto_approve=auto_approve)


def save_audit(report: AuditReport, md_path: str, json_path: Optional[str] = None) -> None:
    Path(md_path).write_text(report.to_markdown(), encoding="utf-8")
    if json_path:
        Path(json_path).write_text(json.dumps({"summary": report.summary(), "cases": [asdict(c) for c in report.cases]},
                                              indent=2, default=str), encoding="utf-8")
