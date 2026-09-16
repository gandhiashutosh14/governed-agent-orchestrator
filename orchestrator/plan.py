"""
Plans and the validator that stands between a planner (LLM or heuristic) and
the runtime. A plan is a small DAG of steps; a step's inputs may reference
earlier outputs with "$step_id.field". Validation is exhaustive and returns
every problem at once so the planner can repair in one round.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence, Set

from .catalog import Catalog

_REF = re.compile(r"^\$([A-Za-z0-9_\-]+)\.([A-Za-z0-9_]+)$")


@dataclass
class PlanStep:
    id: str
    capability: str
    inputs: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def references(self) -> List[tuple]:
        """(step_id, field) pairs this step depends on."""
        refs = []
        for v in self.inputs.values():
            if isinstance(v, str):
                m = _REF.match(v.strip())
                if m:
                    refs.append((m.group(1), m.group(2)))
        return refs

    def depends_on(self) -> Set[str]:
        return {s for s, _ in self.references()}


@dataclass
class Plan:
    objective: str
    steps: List[PlanStep]
    source: str = "unknown"          # which planner produced it: "llm", "heuristic", ...

    def step(self, step_id: str) -> PlanStep:
        for s in self.steps:
            if s.id == step_id:
                return s
        raise KeyError(step_id)

    def to_dict(self) -> Dict[str, Any]:
        return {"objective": self.objective, "source": self.source,
                "steps": [{"id": s.id, "capability": s.capability, "inputs": s.inputs, "reason": s.reason} for s in self.steps]}


class PlanParseError(ValueError):
    pass


def parse_plan(text: str, objective: str, source: str = "llm") -> Plan:
    """Parse a planner reply. Accepts a bare JSON object or one inside a ```json fence."""
    if not text or not text.strip():
        raise PlanParseError("Planner returned an empty reply.")
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    body = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise PlanParseError(f"Planner reply is not valid JSON: {e.msg}") from e
    steps_raw = data.get("steps")
    if not isinstance(steps_raw, list):
        raise PlanParseError("Planner JSON must contain a 'steps' list.")
    steps = []
    for i, s in enumerate(steps_raw):
        if not isinstance(s, dict) or not isinstance(s.get("capability"), str):
            raise PlanParseError(f"Step {i} must be an object with a string 'capability'.")
        inputs = s.get("inputs") or {}
        if not isinstance(inputs, dict):
            raise PlanParseError(f"Step {i} 'inputs' must be a JSON object mapping input names to values, "
                                 f"got {type(inputs).__name__}.")
        steps.append(PlanStep(id=str(s.get("id") or f"s{i + 1}"), capability=s["capability"],
                              inputs=dict(inputs), reason=str(s.get("reason") or "")))
    return Plan(objective=objective, steps=steps, source=source)


def validate_plan(plan: Plan, catalog: Catalog, *, max_steps: int = 8, total_budget_ms: int = 120_000) -> List[str]:
    """Every problem with the plan, as plain sentences. An empty list means the plan may run."""
    errors: List[str] = []
    if not plan.steps:
        return ["Plan has no steps."]
    if len(plan.steps) > max_steps:
        errors.append(f"Plan has {len(plan.steps)} steps; the limit is {max_steps}.")

    ids = [s.id for s in plan.steps]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        errors.append(f"Duplicate step ids: {', '.join(sorted(dupes))}.")
    known_ids = set(ids)

    budget = 0
    for s in plan.steps:
        cap = catalog.get(s.capability)
        if cap is None:
            errors.append(f"Step {s.id} uses unknown capability '{s.capability}'. Available: {', '.join(catalog.names())}.")
            continue
        budget += cap.budget_ms
        missing = cap.missing_inputs(s.inputs)
        if missing:
            errors.append(f"Step {s.id} ({cap.name}) is missing required inputs: {', '.join(missing)}.")
        for k in s.inputs:
            if cap.inputs and k not in cap.inputs:
                errors.append(f"Step {s.id} ({cap.name}) has an input '{k}' the capability does not accept.")
        for ref_step, ref_field in s.references():
            if ref_step not in known_ids:
                errors.append(f"Step {s.id} references unknown step '{ref_step}'.")
                continue
            if ref_step == s.id:
                errors.append(f"Step {s.id} references itself.")
                continue
            target = catalog.get(plan.step(ref_step).capability)
            if target and target.outputs and ref_field not in target.outputs:
                errors.append(f"Step {s.id} references '{ref_step}.{ref_field}' but {target.name} only outputs: {', '.join(target.outputs)}.")
    if budget > total_budget_ms:
        errors.append(f"Plan budget {budget} ms exceeds the run limit of {total_budget_ms} ms.")

    try:
        execution_waves(plan)
    except ValueError as e:
        errors.append(str(e))
    return errors


def execution_waves(plan: Plan) -> List[List[str]]:
    """Group steps into waves: every step in a wave depends only on earlier waves. Raises on cycles."""
    remaining = {s.id: s.depends_on() & {t.id for t in plan.steps} for s in plan.steps}
    waves: List[List[str]] = []
    done: Set[str] = set()
    while remaining:
        ready = [sid for sid, deps in remaining.items() if deps <= done]
        if not ready:
            raise ValueError(f"Plan has a dependency cycle among steps: {', '.join(sorted(remaining))}.")
        waves.append(ready)
        done |= set(ready)
        for sid in ready:
            remaining.pop(sid)
    return waves
