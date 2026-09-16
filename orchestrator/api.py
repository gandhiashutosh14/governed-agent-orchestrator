"""
HTTP surface: start runs, watch their DecisionTrace as Server-Sent Events,
approve or deny paused steps, and review policy rules.

  POST /runs                      {"objective": "..."}      -> run snapshot (may be awaiting_approval)
  GET  /runs/{run_id}                                       -> snapshot
  GET  /runs/{run_id}/events                                -> SSE stream of trace events (replay + live)
  POST /runs/{run_id}/approve     {"approved": true|false}  -> resumed snapshot
  GET  /capabilities
  GET  /policy   POST /policy {"text": ...}   POST /policy/{id}/approve
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .capabilities import default_adapters
from .catalog import Catalog
from .graph import Orchestrator
from .heuristics import build_heuristic_planner
from .planner import LLMPlanner, PlannerCascade
from .policy import PolicyMemory

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build_orchestrator(llm_spec: Optional[str] = None, trace_dir: Optional[str] = None,
                       policy_path: Optional[str] = None) -> tuple:
    catalog = Catalog.load(os.path.join(ROOT, "capabilities.json"))
    memory = PolicyMemory(policy_path or os.path.join(ROOT, "policy_memory.json"))
    planners = []
    if llm_spec and llm_spec != "none":
        from .llm import make_llm
        planners.append(LLMPlanner(make_llm(llm_spec), catalog, memory))
    planners.append(build_heuristic_planner(catalog))
    orch = Orchestrator(catalog, default_adapters(), PlannerCascade(planners), trace_dir=trace_dir)
    return orch, catalog, memory


app = FastAPI(title="Governed agent orchestrator", version="0.1.0")
_state: Dict[str, Any] = {}
_runs: Dict[str, Dict[str, Any]] = {}
_queues: Dict[str, list] = {}


@app.on_event("startup")
async def _startup() -> None:
    orch, catalog, memory = build_orchestrator(os.environ.get("ORCH_LLM", "none"),
                                               trace_dir=os.environ.get("ORCH_TRACE_DIR", os.path.join(ROOT, "traces")))
    _state.update(orch=orch, catalog=catalog, memory=memory)


class StartRun(BaseModel):
    objective: str


class Approval(BaseModel):
    approved: bool
    by: str = "api"


class RuleIn(BaseModel):
    text: str
    scope: str = "global"


def _subscribe(run_id: str) -> None:
    orch: Orchestrator = _state["orch"]
    trace = orch.trace_for(run_id)
    _queues.setdefault(run_id, [])

    def push(ev):
        for q in _queues[run_id]:
            q.put_nowait(ev.to_dict())
    trace.subscribe(push)


@app.post("/runs")
async def start_run(body: StartRun) -> Dict[str, Any]:
    orch: Orchestrator = _state["orch"]
    import uuid
    run_id = uuid.uuid4().hex[:10]
    _subscribe(run_id)
    snap = await orch.start(body.objective, run_id)
    _runs[run_id] = snap
    return snap


@app.get("/runs/{run_id}")
async def get_run(run_id: str) -> Dict[str, Any]:
    if run_id not in _runs:
        raise HTTPException(404, "run not found")
    return _runs[run_id]


@app.post("/runs/{run_id}/approve")
async def approve(run_id: str, body: Approval) -> Dict[str, Any]:
    if run_id not in _runs:
        raise HTTPException(404, "run not found")
    orch: Orchestrator = _state["orch"]
    if not orch.pending_interrupt(run_id):
        raise HTTPException(409, "run is not awaiting approval")
    snap = await orch.resume(run_id, body.approved, by=body.by)
    _runs[run_id] = snap
    return snap


@app.get("/runs/{run_id}/events")
async def events(run_id: str):
    if run_id not in _runs:
        raise HTTPException(404, "run not found")
    orch: Orchestrator = _state["orch"]
    trace = orch.trace_for(run_id)
    q: asyncio.Queue = asyncio.Queue()
    _queues.setdefault(run_id, []).append(q)

    async def gen():
        try:
            for ev in trace.to_list():          # replay history first
                yield f"event: {ev['type']}\ndata: {json.dumps(ev, default=str)}\n\n"
            terminal = {"run_finished", "run_failed"}
            if any(e.type in terminal for e in trace.events):
                return
            while True:
                ev = await q.get()
                yield f"event: {ev['type']}\ndata: {json.dumps(ev, default=str)}\n\n"
                if ev["type"] in terminal:
                    return
        finally:
            _queues[run_id].remove(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/capabilities")
async def capabilities() -> Dict[str, Any]:
    catalog: Catalog = _state["catalog"]
    return {"capabilities": [c.__dict__ for c in catalog]}


@app.get("/policy")
async def policy_rules() -> Dict[str, Any]:
    memory: PolicyMemory = _state["memory"]
    return {"rules": [r.__dict__ for r in memory.rules.values()]}


@app.post("/policy")
async def add_rule(body: RuleIn) -> Dict[str, Any]:
    memory: PolicyMemory = _state["memory"]
    return memory.add(body.text, scope=body.scope).__dict__


@app.post("/policy/{rule_id}/approve")
async def approve_rule(rule_id: str) -> Dict[str, Any]:
    memory: PolicyMemory = _state["memory"]
    if rule_id not in memory.rules:
        raise HTTPException(404, "rule not found")
    return memory.approve(rule_id).__dict__
