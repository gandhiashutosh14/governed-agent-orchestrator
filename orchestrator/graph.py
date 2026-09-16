"""
The LangGraph run: plan → execute → synthesize, with a human-approval
interrupt. State is checkpointed per run (thread_id = run_id) so a run paused
at an approval gate can be resumed later from another process or request.

Design notes:
- The runtime is idempotent over its RunState (completed steps are skipped),
  which is what makes LangGraph's re-execute-the-node-on-resume semantics safe.
- Every decision is journaled in the DecisionTrace independently of the graph
  checkpoint, so the audit trail survives even if the checkpointer is in-memory.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import asdict
from typing import Any, Callable, Dict, List, Optional, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .catalog import Catalog
from .plan import Plan, PlanStep
from .runtime import ApprovalRequired, RunState, Runtime
from .trace import DecisionTrace


class RunGraphState(TypedDict, total=False):
    run_id: str
    objective: str
    plan: Optional[Dict[str, Any]]
    plan_source: str
    run_state: Dict[str, Any]           # serialised RunState
    approvals: List[str]
    denied: List[str]
    status: str                         # planned | awaiting_approval | completed | failed | denied
    answer: str


def _plan_from_dict(d: Dict[str, Any]) -> Plan:
    return Plan(objective=d["objective"], source=d.get("source", "?"),
                steps=[PlanStep(id=s["id"], capability=s["capability"], inputs=dict(s.get("inputs") or {}),
                                reason=s.get("reason", "")) for s in d["steps"]])


def _state_to_dict(rs: RunState) -> Dict[str, Any]:
    return {"outputs": rs.outputs, "completed": sorted(rs.completed), "failed": rs.failed,
            "approvals": sorted(rs.approvals), "fallbacks_used": rs.fallbacks_used, "status": rs.status,
            "call_limit": rs.call_limit, "calls_used": rs.calls_used, "denied": rs.denied}


def _state_from_dict(d: Optional[Dict[str, Any]], call_limit: Optional[int] = None) -> RunState:
    if not d:
        return RunState(call_limit=call_limit)
    return RunState(outputs=dict(d.get("outputs") or {}), completed=set(d.get("completed") or []),
                    failed=dict(d.get("failed") or {}), approvals=set(d.get("approvals") or []),
                    fallbacks_used=int(d.get("fallbacks_used") or 0), status=d.get("status", "pending"),
                    call_limit=d.get("call_limit", call_limit), calls_used=int(d.get("calls_used") or 0),
                    denied=dict(d.get("denied") or {}))


class Orchestrator:
    """Wires planner cascade, catalog, adapters and traces into one compiled LangGraph."""

    def __init__(self, catalog: Catalog, adapters: Dict[str, Callable], planner, *, trace_dir: Optional[str] = None,
                 checkpointer=None, call_limit: Optional[int] = None):
        self.catalog = catalog
        self.adapters = adapters
        self.planner = planner
        self.trace_dir = trace_dir
        self.call_limit = call_limit          # shared tool-call budget per run; None = unlimited
        self.traces: Dict[str, DecisionTrace] = {}
        # Work completed before an approval interrupt. LangGraph discards a node's partial updates when it
        # interrupts, so the runtime state is parked here and picked up when the node re-executes on resume.
        self._partial: Dict[str, Dict[str, Any]] = {}
        self.checkpointer = checkpointer or MemorySaver()
        self.graph = self._build()

    # ------------------------------------------------------------------
    def trace_for(self, run_id: str) -> DecisionTrace:
        if run_id not in self.traces:
            self.traces[run_id] = DecisionTrace(run_id, self.trace_dir)
        return self.traces[run_id]

    def _build(self):
        g = StateGraph(RunGraphState)

        async def plan_node(state: RunGraphState) -> Dict[str, Any]:
            trace = self.trace_for(state["run_id"])
            if not state.get("plan"):
                trace.emit("run_started", objective=state["objective"])
            plan = await asyncio.to_thread(self.planner.plan, state["objective"], trace)
            if plan is None:
                trace.emit("run_failed", reason="no valid plan")
                return {"status": "failed", "plan": None, "answer": "No valid plan could be produced for this objective."}
            return {"plan": plan.to_dict(), "plan_source": plan.source, "status": "planned",
                    "run_state": _state_to_dict(RunState(call_limit=self.call_limit)), "approvals": [], "denied": []}

        async def execute_node(state: RunGraphState) -> Dict[str, Any]:
            if state.get("status") == "failed" or not state.get("plan"):
                return {}
            trace = self.trace_for(state["run_id"])
            plan = _plan_from_dict(state["plan"])
            run_id = state["run_id"]
            rs = _state_from_dict(self._partial.pop(run_id, None) or state.get("run_state"), self.call_limit)
            rs.approvals |= set(state.get("approvals") or [])
            runtime = Runtime(self.catalog, self.adapters, trace)
            while True:
                try:
                    rs = await runtime.run(plan, rs)
                    return {"run_state": _state_to_dict(rs), "status": rs.status}
                except ApprovalRequired as gate:
                    # Pause here. On resume LangGraph re-runs this node; `interrupt` then returns the human's decision.
                    self._partial[run_id] = _state_to_dict(rs)
                    decision = interrupt({"run_id": run_id, "step": gate.step.id,
                                          "capability": gate.step.capability, "inputs": gate.inputs})
                    self._partial.pop(run_id, None)
                    approved = bool(decision.get("approved")) if isinstance(decision, dict) else bool(decision)
                    if approved:
                        trace.emit("approval_granted", step=gate.step.id, by=(decision.get("by") if isinstance(decision, dict) else None))
                        rs.approvals.add(gate.step.id)
                        continue
                    trace.emit("approval_denied", step=gate.step.id)
                    rs.failed[gate.step.id] = "denied by human reviewer"
                    rs.status = "denied"
                    trace.emit("run_finished", completed=sorted(rs.completed), failed=rs.failed, fallbacks_used=rs.fallbacks_used)
                    return {"run_state": _state_to_dict(rs), "status": "denied"}

        async def synthesize_node(state: RunGraphState) -> Dict[str, Any]:
            if not state.get("plan"):
                return {}          # planning failed: the plan node already wrote the answer
            rs = _state_from_dict(state.get("run_state"))
            plan = _plan_from_dict(state["plan"])
            texts: List[str] = []
            for step in plan.steps:
                out = rs.outputs.get(step.id)
                if not out:
                    continue
                if "text" in out:
                    texts = [str(out["text"])]  # a drafted summary supersedes raw facts
                elif "summary" in out and not texts:
                    texts.append(str(out["summary"]))
            answer = " ".join(texts) if texts else "The plan ran but produced no summary."
            if rs.failed:
                answer += " Some steps did not complete: " + "; ".join(f"{k} ({v})" for k, v in rs.failed.items())
            return {"answer": answer}

        g.add_node("plan", plan_node)
        g.add_node("execute", execute_node)
        g.add_node("synthesize", synthesize_node)
        g.add_edge(START, "plan")
        g.add_edge("plan", "execute")
        g.add_edge("execute", "synthesize")
        g.add_edge("synthesize", END)
        return g.compile(checkpointer=self.checkpointer)

    # ------------------------------------------------------------------
    @staticmethod
    def _config(run_id: str) -> Dict[str, Any]:
        return {"configurable": {"thread_id": run_id}}

    async def start(self, objective: str, run_id: Optional[str] = None) -> Dict[str, Any]:
        run_id = run_id or uuid.uuid4().hex[:10]
        result = await self.graph.ainvoke({"run_id": run_id, "objective": objective}, self._config(run_id))
        return self._snapshot(run_id, result)

    async def resume(self, run_id: str, approved: bool, by: str = "reviewer") -> Dict[str, Any]:
        result = await self.graph.ainvoke(Command(resume={"approved": approved, "by": by}), self._config(run_id))
        return self._snapshot(run_id, result)

    def pending_interrupt(self, run_id: str) -> Optional[Dict[str, Any]]:
        snap = self.graph.get_state(self._config(run_id))
        for task in getattr(snap, "tasks", ()):
            for intr in getattr(task, "interrupts", ()):
                return dict(intr.value) if isinstance(intr.value, dict) else {"value": intr.value}
        return None

    def _snapshot(self, run_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
        pending = self.pending_interrupt(run_id)
        status = "awaiting_approval" if pending else result.get("status", "unknown")
        run_state = self._partial.get(run_id) if pending else result.get("run_state")
        return {"run_id": run_id, "objective": result.get("objective"), "status": status, "plan": result.get("plan"),
                "plan_source": result.get("plan_source"), "answer": result.get("answer", ""),
                "run_state": run_state, "pending_approval": pending}
