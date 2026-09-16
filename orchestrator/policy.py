"""
Governed policy memory: rules the planner is told to follow.

Rules are draft by default and are only injected into prompts once a human
approves them. Rules can be written by people or mined from DecisionTraces
(for example, "when X fails, Y was used" becomes a draft suggestion), but the
review gate is the same either way: nothing reaches the planner unreviewed.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .trace import Event


@dataclass
class PolicyRule:
    id: str
    text: str
    scope: str = "global"            # "global" or a capability / use-case name
    status: str = "draft"            # draft | approved | rejected
    source: str = "human"            # human | mined
    created: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    evidence: Dict[str, str] = field(default_factory=dict)


class PolicyMemory:
    def __init__(self, path: Optional[str] = None):
        self._path = Path(path) if path else None
        self.rules: Dict[str, PolicyRule] = {}
        if self._path and self._path.exists():
            for d in json.loads(self._path.read_text(encoding="utf-8")):
                self.rules[d["id"]] = PolicyRule(**d)

    def _save(self) -> None:
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps([asdict(r) for r in self.rules.values()], indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    def add(self, text: str, *, scope: str = "global", source: str = "human", status: str = "draft",
            evidence: Optional[Dict[str, str]] = None) -> PolicyRule:
        rule = PolicyRule(id=uuid.uuid4().hex[:8], text=text.strip(), scope=scope, status=status, source=source,
                          evidence=evidence or {})
        self.rules[rule.id] = rule
        self._save()
        return rule

    def approve(self, rule_id: str) -> PolicyRule:
        rule = self.rules[rule_id]
        rule.status = "approved"
        self._save()
        return rule

    def reject(self, rule_id: str) -> PolicyRule:
        rule = self.rules[rule_id]
        rule.status = "rejected"
        self._save()
        return rule

    def approved(self, scopes: Iterable[str] = ("global",)) -> List[PolicyRule]:
        wanted = set(scopes) | {"global"}
        return [r for r in self.rules.values() if r.status == "approved" and r.scope in wanted]

    def drafts(self) -> List[PolicyRule]:
        return [r for r in self.rules.values() if r.status == "draft"]

    def render(self, scopes: Iterable[str] = ("global",)) -> str:
        rules = self.approved(scopes)
        if not rules:
            return ""
        return "Approved policy rules you must follow:\n" + "\n".join(f"- {r.text}" for r in rules)


def mine_rules(events: List[Event]) -> List[str]:
    """Turn patterns in a trace into draft rule texts. Deliberately conservative: only two patterns."""
    suggestions: List[str] = []
    for ev in events:
        if ev.type == "step_fallback":
            suggestions.append(
                f"When '{ev.data.get('capability')}' fails for objectives like \"{ev.data.get('objective', '')[:60]}\", "
                f"plan '{ev.data.get('fallback')}' directly.")
        if ev.type == "plan_rejected":
            for err in ev.data.get("errors", [])[:2]:
                suggestions.append(f"Avoid this planning mistake: {err}")
    # dedupe, keep order
    seen, out = set(), []
    for s in suggestions:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out
