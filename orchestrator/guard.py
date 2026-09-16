"""
Bounded tool execution: the two checks the runtime makes before it lets a
step call a tool.

1. Argument constraints, declared per input in the capability catalog (trusted
   metadata, never in the plan): numeric bounds, allowed values, allowed e-mail
   domains, a regex, a maximum length.
2. A shared call budget for the whole run. Every tool call, including fallback
   calls, reserves one unit atomically. Child scopes draw from their parent, so
   two children of a ten-call budget can never spend twenty between them.

Both checks are deterministic and produce sentences a reviewer (or a planner
asked to repair its plan) can act on.
"""
from __future__ import annotations

import re
import threading
from typing import Any, Dict, List, Optional

CONSTRAINT_KEYS = {"min", "max", "allowed", "allowed_domains", "pattern", "max_length"}


def validate_constraint_spec(capability: str, constraints: Dict[str, Dict[str, Any]], inputs: Dict[str, str]) -> List[str]:
    """Problems with a capability's constraint block, as sentences. Called when the catalog loads."""
    errors: List[str] = []
    for name, spec in constraints.items():
        if inputs and name not in inputs:
            errors.append(f"Capability '{capability}' constrains input '{name}' which it does not declare.")
        if not isinstance(spec, dict):
            errors.append(f"Capability '{capability}': constraint for '{name}' must be an object.")
            continue
        unknown = set(spec) - CONSTRAINT_KEYS
        if unknown:
            errors.append(f"Capability '{capability}': unknown constraint keys for '{name}': {', '.join(sorted(unknown))}.")
        if "pattern" in spec:
            try:
                re.compile(str(spec["pattern"]))
            except re.error as e:
                errors.append(f"Capability '{capability}': pattern for '{name}' does not compile ({e}).")
    return errors


def check_constraints(constraints: Dict[str, Dict[str, Any]], inputs: Dict[str, Any]) -> List[str]:
    """Every violated constraint for these resolved inputs. Empty list means the call may proceed.

    Inputs that are absent are not checked here (required inputs are the validator's job);
    inputs that are unresolved references ("$step.field") are skipped so the validator can call
    this on literal inputs before execution and the runtime can call it again after resolution.
    """
    violations: List[str] = []
    for name, spec in constraints.items():
        if name not in inputs:
            continue
        value = inputs[name]
        if isinstance(value, str) and value.startswith("$"):
            continue
        if "min" in spec or "max" in spec:
            try:
                number = float(value)
            except (TypeError, ValueError):
                violations.append(f"Input '{name}' must be a number, got {value!r}.")
                continue
            if "min" in spec and number < float(spec["min"]):
                violations.append(f"Input '{name}' is {value!r}; the minimum is {spec['min']}.")
            if "max" in spec and number > float(spec["max"]):
                violations.append(f"Input '{name}' is {value!r}; the maximum is {spec['max']}.")
        if "allowed" in spec and value not in spec["allowed"]:
            violations.append(f"Input '{name}' is {value!r}; allowed values: {', '.join(map(str, spec['allowed']))}.")
        if "allowed_domains" in spec:
            text = str(value).strip().lower()
            domain = text.rpartition("@")[2] if "@" in text else ""
            if not domain or domain not in [d.lower() for d in spec["allowed_domains"]]:
                violations.append(f"Input '{name}' is {value!r}, which is not an address in an allowed domain "
                                  f"({', '.join(spec['allowed_domains'])}).")
        if "pattern" in spec and not re.fullmatch(str(spec["pattern"]), str(value)):
            violations.append(f"Input '{name}' is {value!r}; it must match /{spec['pattern']}/.")
        if "max_length" in spec and len(str(value)) > int(spec["max_length"]):
            violations.append(f"Input '{name}' is {len(str(value))} characters long; the maximum is {spec['max_length']}.")
    return violations


class BudgetExhausted(Exception):
    pass


class RunBudget:
    """A call budget shared by everything that runs inside one run.

    `limit=None` means unlimited (used and reported, never denied). `child(limit)` returns a scope
    that is capped by its own limit *and* by every ancestor: a reservation walks up the chain,
    checks every level, and commits on every level under one lock, so siblings cannot overspend
    their parent no matter how they interleave.
    """

    def __init__(self, limit: Optional[int] = None, *, used: int = 0, parent: Optional["RunBudget"] = None,
                 name: str = "run"):
        if limit is not None and limit < 0:
            raise ValueError("budget limit must be >= 0")
        self.limit = limit
        self.used = used
        self.parent = parent
        self.name = name
        self._lock = parent._lock if parent else threading.Lock()
        self.denied = 0

    # ------------------------------------------------------------------
    @property
    def remaining(self) -> Optional[int]:
        if self.limit is None:
            return None
        return max(0, self.limit - self.used)

    def child(self, limit: Optional[int] = None, name: str = "child") -> "RunBudget":
        return RunBudget(limit, parent=self, name=name)

    def reserve(self, n: int = 1) -> bool:
        """Atomically take `n` units from this scope and every ancestor. False, and nothing taken, if any level lacks room."""
        if n <= 0:
            raise ValueError("reserve at least one unit")
        with self._lock:
            scope: Optional[RunBudget] = self
            while scope is not None:
                if scope.limit is not None and scope.used + n > scope.limit:
                    self.denied += 1
                    return False
                scope = scope.parent
            scope = self
            while scope is not None:
                scope.used += n
                scope = scope.parent
            return True

    def release(self, n: int = 1) -> None:
        """Give units back (a reserved call that never happened). Never below zero at any level."""
        with self._lock:
            scope: Optional[RunBudget] = self
            while scope is not None:
                scope.used = max(0, scope.used - n)
                scope = scope.parent

    def snapshot(self) -> Dict[str, Any]:
        return {"scope": self.name, "limit": self.limit, "used": self.used, "remaining": self.remaining}
