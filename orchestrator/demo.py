"""
Guard demo: three scripted scenarios that exercise bounded tool execution and
write what the trace recorded into a Markdown report.

  1. allowed   — a report to an address in the allowed domain: approval gate, then delivery.
  2. denied    — the same objective addressed outside the allowed domain: refused before
                 anything runs, with the reason in the trace.
  3. exhausted — a hand-written plan with two independent steps and a run budget of one call:
                 exactly one sibling runs, the other is denied, the dependant is blocked.

The planner in scenarios 1 and 2 is the deterministic heuristic planner; scenario 3 uses a
plan written in this file. No model is involved anywhere. The report is evidence of what the
reference monitor did on this code, not of planner quality.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .capabilities import default_adapters
from .catalog import Catalog
from .graph import Orchestrator
from .heuristics import build_heuristic_planner
from .plan import Plan, PlanStep, validate_plan
from .planner import PlannerCascade
from .runtime import RunState, Runtime
from .trace import DecisionTrace, Event

ROOT = Path(__file__).resolve().parent.parent
SHOWN_EVENTS = {"plan_accepted", "plan_proposed", "plan_rejected", "planner_fallback", "approval_required",
                "approval_granted", "approval_denied", "tool_call_allowed", "tool_call_denied", "budget_exhausted",
                "step_finished", "step_failed", "run_finished", "run_failed"}


@dataclass
class Scenario:
    name: str
    description: str
    status: str
    events: List[Event]
    notes: List[str] = field(default_factory=list)


def _revision() -> str:
    try:
        sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True).strip()
        dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()
        return sha + (" (uncommitted changes present)" if dirty else "")
    except Exception:  # noqa: BLE001
        return "unknown"


def _summarise(ev: Event) -> str:
    d = ev.data
    if ev.type == "tool_call_allowed":
        b = d.get("budget") or {}
        return f"{d.get('capability')} effect={d.get('effect')} budget {b.get('used')}/{b.get('limit')}"
    if ev.type == "tool_call_denied":
        why = " ".join(d.get("violations") or []) if d.get("reason") == "constraint" else json.dumps(d.get("budget"))
        return f"{d.get('capability')} reason={d.get('reason')}: {why}"
    if ev.type in ("plan_rejected",):
        return "; ".join(d.get("errors") or [])
    if ev.type in ("plan_proposed", "plan_accepted"):
        steps = (d.get("plan") or {}).get("steps") or []
        return f"{d.get('planner')}: " + " -> ".join(s["capability"] for s in steps) + (f"; errors: {'; '.join(d['errors'])}" if d.get("errors") else "")
    if ev.type == "approval_required":
        return f"{d.get('capability')} recipient={d.get('inputs', {}).get('recipient')} effect={d.get('effect')}"
    if ev.type == "step_finished":
        return f"{d.get('capability')} in {d.get('ms')} ms" + (" (fallback)" if d.get("via_fallback") else "")
    if ev.type == "step_failed":
        return f"{d.get('capability')}: {d.get('error')}"
    if ev.type in ("run_finished", "run_failed"):
        return f"completed={d.get('completed')} failed={list((d.get('failed') or {}).keys())} budget={json.dumps(d.get('budget'))}"
    if ev.type == "budget_exhausted":
        return json.dumps(d.get("budget"))
    return json.dumps({k: v for k, v in d.items() if k != "plan"}, default=str)[:160]


async def _run_objective(catalog: Catalog, objective: str, *, approve: bool, call_limit: Optional[int] = None) -> Scenario:
    orch = Orchestrator(catalog, default_adapters(), PlannerCascade([build_heuristic_planner(catalog)]), call_limit=call_limit)
    snap = await orch.start(objective)
    if snap["status"] == "awaiting_approval" and approve:
        snap = await orch.resume(snap["run_id"], True, by="demo")
    trace = orch.trace_for(snap["run_id"])
    return Scenario(name="", description=objective, status=snap["status"], events=list(trace.events))


async def scenario_allowed(catalog: Catalog) -> Scenario:
    s = await _run_objective(catalog, "Forecast next year's revenue and send it to finance@example.com.", approve=True)
    s.name = "1. allowed"
    s.notes = ["Recipient is in the allowed domain (example.com); the irreversible send waits for approval, "
               "the demo approves it, and every call is journaled with its effect class and budget use. "
               "`approval_required` appears twice because LangGraph re-executes the node on resume and the runtime "
               "reaches the gate again before the recorded decision is applied; the second one is the resume, not a second ask."]
    return s


async def scenario_denied(catalog: Catalog) -> Scenario:
    s = await _run_objective(catalog, "Forecast next year's revenue and send it to finance@evil-example.org.", approve=True)
    s.name = "2. denied"
    s.notes = ["Recipient is outside the allowed domain. The validator applies the catalog constraint to the literal "
               "argument, so the plan is rejected before any tool runs; the run ends with no valid plan."]
    return s


async def scenario_exhausted(catalog: Catalog) -> Scenario:
    plan = Plan("Two independent lookups, then a summary, under a one-call budget.", source="scripted", steps=[
        PlanStep("s1", "revenue_by_year", {}, reason="yearly totals"),
        PlanStep("s2", "top_genres_by_tracks_sold", {"top_n": 3}, reason="genre ranking"),
        PlanStep("s3", "draft_summary", {"objective": "Summarise both", "facts": "$s1.summary"}, reason="summary"),
    ])
    errors = validate_plan(plan, catalog)
    assert not errors, errors
    trace = DecisionTrace("demo-exhausted")
    rt = Runtime(catalog, default_adapters(), trace)
    state = await rt.run(plan, RunState(call_limit=1))
    s = Scenario(name="3. exhausted", description=plan.objective, status=state.status, events=list(trace.events))
    fate = {sid: ("completed" if sid in state.completed else state.failed.get(sid, "?").rstrip(".")) for sid in ("s1", "s2", "s3")}
    s.notes = ["s1 and s2 are siblings in the first wave and race for one shared unit of budget. Which sibling wins is "
               "not fixed; that exactly one wins is. s3 depends on s1: if s1 won it reaches the guard and is denied for "
               "budget, if s1 lost it is blocked before the guard. In this run: "
               + "; ".join(f"{k}: {v}" for k, v in fate.items()) + "."]
    return s


def render(scenarios: List[Scenario], command: str) -> str:
    lines = ["# Guard demo: bounded tool execution", "",
             f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} at revision {_revision()} with `{command}`. "
             "Planner: deterministic heuristic rules (scenarios 1 and 2) and a plan written in `orchestrator/demo.py` "
             "(scenario 3). No language model was involved.", ""]
    for s in scenarios:
        lines += [f"## {s.name}: {s.description}", "", f"Status: `{s.status}`", ""]
        for n in s.notes:
            lines += [n, ""]
        lines += ["| seq | event | detail |", "|---|---|---|"]
        for ev in s.events:
            if ev.type in SHOWN_EVENTS:
                lines.append(f"| {ev.seq} | {ev.type} | {_summarise(ev).replace('|', '/')} |")
        lines.append("")
    return "\n".join(lines)


async def run_demo(catalog: Catalog) -> List[Scenario]:
    return [await scenario_allowed(catalog), await scenario_denied(catalog), await scenario_exhausted(catalog)]


def main(report_path: str, catalog_path: Optional[str] = None) -> int:
    catalog = Catalog.load(catalog_path or str(ROOT / "capabilities.json"))
    scenarios = asyncio.run(run_demo(catalog))
    text = render(scenarios, "orchestrator demo-guard --report " + report_path)
    Path(report_path).parent.mkdir(parents=True, exist_ok=True)
    Path(report_path).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "reports/guard-demo.md"))
