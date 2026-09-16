"""
Runtime: executes a validated plan in dependency waves. Steps in the same
wave run concurrently; each step gets its capability's budget as a timeout,
falls back to the declared fallback on failure, and stops the run at an
approval gate when the capability requires a human decision.

Before any tool is called the runtime applies the guard: the resolved
arguments must satisfy the capability's constraints and one unit of the run's
shared call budget must be reserved. Both outcomes are journaled as
tool_call_allowed / tool_call_denied events, so a trace shows not only what
ran but what was refused and why.
"""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Union

from .catalog import Capability, Catalog
from .guard import RunBudget, check_constraints
from .plan import Plan, PlanStep, execution_waves
from .trace import DecisionTrace, Stopwatch

Adapter = Callable[[Dict[str, Any]], Union[Dict[str, Any], Awaitable[Dict[str, Any]]]]


class ApprovalRequired(Exception):
    def __init__(self, step: PlanStep, inputs: Dict[str, Any]):
        super().__init__(f"Step {step.id} ({step.capability}) requires human approval")
        self.step = step
        self.inputs = inputs


class StepError(Exception):
    pass


class CallDenied(Exception):
    """The guard refused a tool call. Not retried and not sent to a fallback: the plan itself is at fault."""

    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass
class RunState:
    outputs: Dict[str, Dict[str, Any]] = field(default_factory=dict)   # step_id -> output fields
    completed: Set[str] = field(default_factory=set)
    failed: Dict[str, str] = field(default_factory=dict)
    approvals: Set[str] = field(default_factory=set)                   # step ids approved by a human
    fallbacks_used: int = 0
    status: str = "pending"
    call_limit: Optional[int] = None                                   # None = unlimited
    calls_used: int = 0
    denied: Dict[str, str] = field(default_factory=dict)               # step_id -> denial reason
    budget: Optional[RunBudget] = field(default=None, repr=False, compare=False)

    def ensure_budget(self) -> RunBudget:
        if self.budget is None:
            self.budget = RunBudget(self.call_limit, used=self.calls_used)
        return self.budget


def resolve_inputs(step: PlanStep, state: RunState) -> Dict[str, Any]:
    resolved: Dict[str, Any] = {}
    for k, v in step.inputs.items():
        if isinstance(v, str) and v.startswith("$"):
            sid, _, fld = v[1:].partition(".")
            if sid not in state.outputs:
                raise StepError(f"Input '{k}' references step '{sid}' which has no output.")
            if fld not in state.outputs[sid]:
                raise StepError(f"Input '{k}' references '{sid}.{fld}' but that step produced: {', '.join(state.outputs[sid])}.")
            resolved[k] = state.outputs[sid][fld]
        else:
            resolved[k] = v
    return resolved


async def _call(adapter: Adapter, inputs: Dict[str, Any], timeout_s: float) -> Dict[str, Any]:
    result = adapter(inputs)
    if inspect.isawaitable(result):
        result = await asyncio.wait_for(result, timeout=timeout_s)
    else:
        # sync adapters run in a worker thread so a slow one cannot block the wave
        result = await asyncio.wait_for(asyncio.to_thread(lambda: result), timeout=timeout_s)
    if not isinstance(result, dict):
        raise StepError(f"Adapter returned {type(result).__name__}, expected a dict of outputs.")
    return result


class Runtime:
    def __init__(self, catalog: Catalog, adapters: Dict[str, Adapter], trace: DecisionTrace):
        self.catalog = catalog
        self.adapters = adapters
        self.trace = trace
        missing = [c.name for c in catalog if c.name not in adapters]
        if missing:
            raise ValueError(f"No adapter registered for capabilities: {', '.join(missing)}")

    # ------------------------------------------------------------------
    def _guard(self, step: PlanStep, cap: Capability, inputs: Dict[str, Any], state: RunState, *, via_fallback: bool = False) -> None:
        """Constraints, then budget. Raises CallDenied after journaling the denial."""
        violations = check_constraints(cap.constraints, inputs)
        if violations:
            self.trace.emit("tool_call_denied", step=step.id, capability=cap.name, reason="constraint",
                            violations=violations, via_fallback=via_fallback)
            raise CallDenied("constraint", " ".join(violations))
        budget = state.ensure_budget()
        if not budget.reserve(1):
            self.trace.emit("tool_call_denied", step=step.id, capability=cap.name, reason="budget",
                            budget=budget.snapshot(), via_fallback=via_fallback)
            if budget.denied == 1:
                self.trace.emit("budget_exhausted", budget=budget.snapshot(), step=step.id)
            raise CallDenied("budget", f"run call budget of {budget.limit} exhausted before step {step.id} ({cap.name}).")
        state.calls_used = budget.used
        self.trace.emit("tool_call_allowed", step=step.id, capability=cap.name, effect=cap.effect,
                        compensation=cap.compensation, budget=budget.snapshot(), via_fallback=via_fallback)

    async def run_step(self, plan: Plan, step: PlanStep, state: RunState) -> None:
        cap = self.catalog.get(step.capability)
        assert cap is not None
        inputs = resolve_inputs(step, state)
        if cap.requires_approval and step.id not in state.approvals:
            self.trace.emit("approval_required", step=step.id, capability=cap.name, inputs=inputs, effect=cap.effect)
            raise ApprovalRequired(step, inputs)

        try:
            self._guard(step, cap, inputs, state)
        except CallDenied as d:
            state.failed[step.id] = f"denied ({d.reason}): {d.detail}"
            state.denied[step.id] = d.reason
            return

        sw = Stopwatch()
        self.trace.emit("step_started", step=step.id, capability=cap.name, inputs=inputs)
        try:
            out = await _call(self.adapters[cap.name], inputs, cap.budget_ms / 1000)
            state.outputs[step.id] = out
            state.completed.add(step.id)
            self.trace.emit("step_finished", step=step.id, capability=cap.name, ms=sw.ms(), outputs=_preview(out))
            return
        except ApprovalRequired:
            raise
        except Exception as e:  # noqa: BLE001 - every failure is journaled, then fallback or fail
            err = f"{type(e).__name__}: {e}" if not isinstance(e, asyncio.TimeoutError) else f"Timeout after {cap.budget_ms} ms"
            self.trace.emit("step_failed", step=step.id, capability=cap.name, ms=sw.ms(), error=err)

        if cap.fallback:
            fb = self.catalog.get(cap.fallback)
            assert fb is not None
            self.trace.emit("step_fallback", step=step.id, capability=cap.name, fallback=fb.name, objective=plan.objective)
            state.fallbacks_used += 1
            fb_inputs = {k: v for k, v in inputs.items() if not fb.inputs or k in fb.inputs}
            try:
                self._guard(step, fb, fb_inputs, state, via_fallback=True)
            except CallDenied as d:
                state.failed[step.id] = f"{err}; fallback denied ({d.reason}): {d.detail}"
                state.denied[step.id] = d.reason
                return
            sw = Stopwatch()
            try:
                out = await _call(self.adapters[fb.name], fb_inputs, fb.budget_ms / 1000)
                state.outputs[step.id] = out
                state.completed.add(step.id)
                self.trace.emit("step_finished", step=step.id, capability=fb.name, ms=sw.ms(), outputs=_preview(out), via_fallback=True)
                return
            except Exception as e:  # noqa: BLE001
                self.trace.emit("step_failed", step=step.id, capability=fb.name, ms=sw.ms(), error=f"{type(e).__name__}: {e}")
        state.failed[step.id] = err

    # ------------------------------------------------------------------
    async def run(self, plan: Plan, state: Optional[RunState] = None) -> RunState:
        """Execute all waves. Raises ApprovalRequired to pause; call again with the step approved to resume."""
        state = state or RunState()
        state.ensure_budget()
        state.status = "running"
        for wave in execution_waves(plan):
            pending = [plan.step(sid) for sid in wave if sid not in state.completed and sid not in state.failed]
            if not pending:
                continue
            self.trace.emit("wave_started", steps=[s.id for s in pending])
            results = await asyncio.gather(*(self.run_step(plan, s, state) for s in pending), return_exceptions=True)
            for r in results:
                if isinstance(r, ApprovalRequired):
                    state.status = "awaiting_approval"
                    raise r
                if isinstance(r, BaseException):
                    raise r
            blocked = [s.id for s in plan.steps if s.id not in state.completed and s.depends_on() & set(state.failed)]
            for sid in blocked:
                state.failed.setdefault(sid, "blocked by a failed dependency")
        state.status = "failed" if state.failed else "completed"
        self.trace.emit("run_finished" if not state.failed else "run_failed", completed=sorted(state.completed),
                        failed=state.failed, denied=state.denied, fallbacks_used=state.fallbacks_used,
                        budget=state.ensure_budget().snapshot())
        return state


def _preview(out: Dict[str, Any], limit: int = 200) -> Dict[str, Any]:
    prev = {}
    for k, v in out.items():
        s = str(v)
        prev[k] = s if len(s) <= limit else s[:limit] + "..."
    return prev
