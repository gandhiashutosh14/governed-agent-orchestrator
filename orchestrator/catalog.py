"""
Capability catalog: the typed contract every plan is validated against.

A capability declares what it needs, what it produces, how long it may take,
whether a human must approve it, what to fall back to, what kind of side
effect it has, and what argument values it accepts. The catalog is data
(JSON), rendered into the planner prompt, so adding an agent never touches the
orchestrator code. Because it is loaded by the operator and not written by the
planner, everything in it is trusted metadata: a plan cannot relabel an
irreversible tool as reversible or widen a constraint.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .guard import validate_constraint_spec

EFFECTS = ("reversible", "compensable", "irreversible")


@dataclass(frozen=True)
class Capability:
    name: str
    description: str
    inputs: Dict[str, str] = field(default_factory=dict)      # name -> type ("string", "number", "list", "object")
    required: List[str] = field(default_factory=list)          # required input names
    outputs: List[str] = field(default_factory=list)           # output field names
    budget_ms: int = 10_000
    requires_approval: bool = False
    fallback: Optional[str] = None                             # capability to try if this one fails
    tags: List[str] = field(default_factory=list)
    effect: str = "reversible"                                 # reversible | compensable | irreversible
    compensation: Optional[str] = None                         # capability that compensates a compensable effect
    constraints: Dict[str, Dict[str, Any]] = field(default_factory=dict)   # input name -> {min, max, allowed, ...}

    def missing_inputs(self, provided: Dict[str, object]) -> List[str]:
        return [r for r in self.required if r not in provided or provided[r] in (None, "")]

    def render(self) -> str:
        ins = ", ".join(f"{k}: {v}{'*' if k in self.required else ''}" for k, v in self.inputs.items()) or "none"
        outs = ", ".join(self.outputs) or "none"
        flags = []
        if self.requires_approval:
            flags.append("REQUIRES HUMAN APPROVAL")
        if self.effect != "reversible":
            flags.append(f"effect={self.effect}" + (f", compensated by {self.compensation}" if self.compensation else ""))
        if self.fallback:
            flags.append(f"fallback={self.fallback}")
        for name, spec in self.constraints.items():
            flags.append(f"{name} " + ", ".join(f"{k}={v}" for k, v in spec.items()))
        tail = f"  [{'; '.join(flags)}]" if flags else ""
        return f"- {self.name}: {self.description}\n    inputs({ins}) -> outputs({outs}); budget {self.budget_ms} ms{tail}"


class Catalog:
    def __init__(self, capabilities: Sequence[Capability]):
        self._caps: Dict[str, Capability] = {}
        for c in capabilities:
            if c.name in self._caps:
                raise ValueError(f"Duplicate capability '{c.name}'")
            self._caps[c.name] = c
        errors: List[str] = []
        for c in self._caps.values():
            if c.fallback and c.fallback not in self._caps:
                errors.append(f"Capability '{c.name}' falls back to unknown capability '{c.fallback}'.")
            if c.effect not in EFFECTS:
                errors.append(f"Capability '{c.name}' has effect '{c.effect}'; expected one of {', '.join(EFFECTS)}.")
            if c.effect == "irreversible" and not c.requires_approval:
                errors.append(f"Capability '{c.name}' is irreversible and must therefore require approval.")
            if c.effect == "compensable" and not c.compensation:
                errors.append(f"Capability '{c.name}' is compensable but names no compensation capability.")
            if c.compensation and c.compensation not in self._caps:
                errors.append(f"Capability '{c.name}' is compensated by unknown capability '{c.compensation}'.")
            if c.compensation and c.effect != "compensable":
                errors.append(f"Capability '{c.name}' names a compensation but its effect is '{c.effect}'.")
            errors.extend(validate_constraint_spec(c.name, c.constraints, c.inputs))
        if errors:
            raise ValueError("Invalid capability catalog:\n" + "\n".join(f"- {e}" for e in errors))

    @classmethod
    def load(cls, path: str) -> "Catalog":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls([Capability(**d) for d in data])

    def get(self, name: str) -> Optional[Capability]:
        return self._caps.get(name)

    def names(self) -> List[str]:
        return list(self._caps)

    def __len__(self) -> int:
        return len(self._caps)

    def __iter__(self):
        return iter(self._caps.values())

    def render(self) -> str:
        """Prompt-ready listing. Required inputs are marked with *."""
        return "\n".join(c.render() for c in self._caps.values())
