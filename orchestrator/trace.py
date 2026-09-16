"""
DecisionTrace: an append-only journal of everything the orchestrator decided
and observed for one run. Every event has a monotonic sequence number, a
timestamp and a type, so a run can be replayed, audited, or mined for policy
rules after the fact. Persisted as JSONL, one file per run.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

EVENT_TYPES = {
    "run_started", "plan_proposed", "plan_rejected", "plan_accepted", "planner_fallback",
    "wave_started", "step_started", "step_finished", "step_failed", "step_fallback",
    "approval_required", "approval_granted", "approval_denied",
    "run_finished", "run_failed", "note",
}


@dataclass
class Event:
    seq: int
    ts: str
    run_id: str
    type: str
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class DecisionTrace:
    def __init__(self, run_id: str, directory: Optional[str] = None):
        self.run_id = run_id
        self.events: List[Event] = []
        self._seq = 0
        self._lock = threading.Lock()
        self._path = Path(directory) / f"{run_id}.jsonl" if directory else None
        self._listeners: List[Callable[[Event], None]] = []
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)

    def subscribe(self, fn: Callable[[Event], None]) -> None:
        self._listeners.append(fn)

    def emit(self, type: str, **data: Any) -> Event:
        if type not in EVENT_TYPES:
            raise ValueError(f"Unknown event type '{type}'")
        with self._lock:
            self._seq += 1
            ev = Event(seq=self._seq, ts=datetime.now(timezone.utc).isoformat(), run_id=self.run_id, type=type, data=data)
            self.events.append(ev)
            if self._path:
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(ev.to_dict(), default=str) + "\n")
        for fn in list(self._listeners):
            fn(ev)
        return ev

    def of_type(self, type: str) -> List[Event]:
        return [e for e in self.events if e.type == type]

    def to_list(self) -> List[Dict[str, Any]]:
        return [e.to_dict() for e in self.events]

    @staticmethod
    def load(path: str) -> List[Event]:
        events = []
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                d = json.loads(line)
                events.append(Event(**d))
        return events


class Stopwatch:
    def __init__(self):
        self.t0 = time.perf_counter()

    def ms(self) -> int:
        return int((time.perf_counter() - self.t0) * 1000)
