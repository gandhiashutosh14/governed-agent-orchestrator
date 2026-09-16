"""
Planners turn an objective into a Plan. Two are provided and composed as a
cascade: an LLM planner that must produce JSON the validator accepts, and a
deterministic heuristic planner used when there is no model or the model's
plan is rejected twice. The cascade records which planner won so the audit can
count fallbacks honestly.
"""
from __future__ import annotations

import json
import re
from typing import Callable, Dict, List, Optional, Protocol, Sequence

from .catalog import Catalog
from .plan import Plan, PlanParseError, PlanStep, parse_plan, validate_plan
from .policy import PolicyMemory
from .trace import DecisionTrace


class LLM(Protocol):
    name: str

    def complete(self, messages: List[Dict[str, str]]) -> str: ...


PLANNER_SYSTEM = """You are a planning agent. Turn the user's objective into a short plan of steps that use ONLY the capabilities listed.

Output a single JSON object: {"steps": [{"id": "s1", "capability": "<name>", "inputs": {...}, "reason": "<why>"}, ...]}
Rules:
- Use only listed capability names and only their declared inputs. Provide every required input (marked *).
- To pass an earlier step's output, use the string "$<step_id>.<output_field>".
- Prefer the fewest steps that fully answer the objective. Never invent data; look it up with a capability.
- Return only the JSON."""


def planner_messages(objective: str, catalog: Catalog, memory: Optional[PolicyMemory], errors: Sequence[str] = ()):
    user = f"Capabilities:\n{catalog.render()}\n\n"
    if memory:
        rules = memory.render()
        if rules:
            user += rules + "\n\n"
    user += f"Objective: {objective}"
    if errors:
        user += "\n\nYour previous plan was rejected:\n" + "\n".join(f"- {e}" for e in errors) + "\nReturn a corrected plan."
    return [{"role": "system", "content": PLANNER_SYSTEM}, {"role": "user", "content": user}]


class LLMPlanner:
    name = "llm"

    def __init__(self, llm: LLM, catalog: Catalog, memory: Optional[PolicyMemory] = None, max_rounds: int = 2):
        self.llm = llm
        self.catalog = catalog
        self.memory = memory
        self.max_rounds = max_rounds

    def plan(self, objective: str, trace: Optional[DecisionTrace] = None) -> Optional[Plan]:
        errors: List[str] = []
        for _ in range(self.max_rounds):
            reply = self.llm.complete(planner_messages(objective, self.catalog, self.memory, errors))
            try:
                plan = parse_plan(reply, objective, source=f"llm:{getattr(self.llm, 'name', '?')}")
            except PlanParseError as e:
                errors = [str(e)]
                if trace:
                    trace.emit("plan_rejected", planner=self.name, errors=errors, reply=reply[:500])
                continue
            errors = validate_plan(plan, self.catalog)
            if trace:
                trace.emit("plan_proposed", planner=self.name, plan=plan.to_dict(), errors=errors)
            if not errors:
                return plan
            if trace:
                trace.emit("plan_rejected", planner=self.name, errors=errors)
        return None


class HeuristicPlanner:
    """Keyword rules → plan. Deterministic, no model, always produces a valid plan for the demo domain."""

    name = "heuristic"

    def __init__(self, catalog: Catalog, rules: Sequence[tuple] = ()):
        # rules: (regex, builder(objective, match) -> list[PlanStep])
        self.catalog = catalog
        self.rules: List[tuple] = list(rules)

    def add_rule(self, pattern: str, builder: Callable) -> None:
        self.rules.append((re.compile(pattern, re.IGNORECASE), builder))

    def plan(self, objective: str, trace: Optional[DecisionTrace] = None) -> Optional[Plan]:
        for pattern, builder in self.rules:
            m = pattern.search(objective)
            if m:
                steps = builder(objective, m)
                plan = Plan(objective=objective, steps=steps, source="heuristic")
                errors = validate_plan(plan, self.catalog)
                if trace:
                    trace.emit("plan_proposed", planner=self.name, plan=plan.to_dict(), errors=errors)
                if not errors:
                    return plan
        return None


class PlannerCascade:
    """Try planners in order; the first valid plan wins. Records a planner_fallback event when not the first."""

    def __init__(self, planners: Sequence[object]):
        self.planners = list(planners)

    def plan(self, objective: str, trace: Optional[DecisionTrace] = None) -> Optional[Plan]:
        for i, p in enumerate(self.planners):
            plan = p.plan(objective, trace)
            if plan is not None:
                if i > 0 and trace:
                    trace.emit("planner_fallback", fell_back_to=getattr(p, "name", "?"), position=i)
                if trace:
                    trace.emit("plan_accepted", planner=getattr(p, "name", "?"), plan=plan.to_dict())
                return plan
        return None
