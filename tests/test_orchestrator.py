import asyncio
import json
import pathlib

import pytest

from orchestrator.audit import run_audit
from orchestrator.capabilities import default_adapters, forecast_next_year, search_policies
from orchestrator.catalog import Catalog
from orchestrator.graph import Orchestrator
from orchestrator.heuristics import build_heuristic_planner
from orchestrator.llm import MockLLM
from orchestrator.plan import Plan, PlanStep, execution_waves, parse_plan, validate_plan
from orchestrator.planner import LLMPlanner, PlannerCascade
from orchestrator.policy import PolicyMemory, mine_rules
from orchestrator.runtime import ApprovalRequired, RunState, Runtime
from orchestrator.trace import DecisionTrace

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def catalog():
    return Catalog.load(str(ROOT / "capabilities.json"))


# ---------------------------------------------------------------------------
# Catalog and plan validation
# ---------------------------------------------------------------------------
def test_catalog_renders_contracts_and_flags(catalog):
    text = catalog.render()
    assert "send_report" in text and "REQUIRES HUMAN APPROVAL" in text
    assert "series: list*" in text  # required inputs are starred


def test_validate_plan_reports_every_problem_at_once(catalog):
    plan = Plan("x", [
        PlanStep("s1", "no_such_cap", {}),
        PlanStep("s2", "forecast_next_year", {}),                      # missing required 'series'
        PlanStep("s3", "draft_summary", {"objective": "x", "facts": "$s9.summary"}),   # unknown step ref
        PlanStep("s4", "revenue_by_country", {"colour": "red"}),        # undeclared input
        PlanStep("s5", "draft_summary", {"objective": "x", "facts": "$s1.nope"}),
    ])
    errors = validate_plan(plan, catalog)
    joined = " ".join(errors)
    assert "unknown capability 'no_such_cap'" in joined
    assert "missing required inputs: series" in joined
    assert "unknown step 's9'" in joined
    assert "input 'colour'" in joined
    assert len(errors) >= 4


def test_validate_plan_catches_cycles_and_bad_output_refs(catalog):
    cyc = Plan("x", [PlanStep("a", "draft_summary", {"objective": "x", "facts": "$b.text"}),
                     PlanStep("b", "draft_summary", {"objective": "x", "facts": "$a.text"})])
    assert any("cycle" in e for e in validate_plan(cyc, catalog))
    bad = Plan("x", [PlanStep("s1", "revenue_by_year", {}),
                     PlanStep("s2", "forecast_next_year", {"series": "$s1.rows"})])   # revenue_by_year outputs 'series'
    assert any("only outputs: series, summary" in e for e in validate_plan(bad, catalog))


def test_execution_waves_group_independent_steps():
    plan = Plan("x", [PlanStep("a", "c", {}), PlanStep("b", "c", {}), PlanStep("c", "c", {"x": "$a.y", "z": "$b.y"})])
    assert execution_waves(plan) == [["a", "b"], ["c"]]


def test_parse_plan_accepts_fenced_json_and_rejects_garbage():
    plan = parse_plan('Here you go:\n```json\n{"steps": [{"id": "s1", "capability": "revenue_by_year", "inputs": {}}]}\n```', "obj")
    assert plan.steps[0].capability == "revenue_by_year"
    with pytest.raises(Exception):
        parse_plan("not json at all", "obj")
    # Seen from a 1.5B planner: inputs given as a string. Must be a repairable parse error, not a crash.
    with pytest.raises(Exception, match="inputs.*must be a JSON object"):
        parse_plan('{"steps": [{"id": "s1", "capability": "revenue_by_year", "inputs": "revenue_by_year"}]}', "obj")
    with pytest.raises(Exception, match="string 'capability'"):
        parse_plan('{"steps": [{"id": "s1", "capability": {"name": "x"}}]}', "obj")


# ---------------------------------------------------------------------------
# Planners
# ---------------------------------------------------------------------------
def test_heuristic_planner_covers_forecast_with_delivery(catalog):
    hp = build_heuristic_planner(catalog)
    plan = hp.plan("Forecast next year's revenue and send it to finance@example.com.")
    assert [s.capability for s in plan.steps] == ["revenue_by_year", "forecast_next_year", "draft_summary", "send_report"]
    assert plan.steps[3].inputs["recipient"] == "finance@example.com"
    assert validate_plan(plan, catalog) == []


def test_llm_planner_repairs_once_then_cascade_falls_back(catalog):
    bad = json.dumps({"steps": [{"id": "s1", "capability": "revenue_by_yer", "inputs": {}}]})
    good = json.dumps({"steps": [{"id": "s1", "capability": "revenue_by_year", "inputs": {}},
                                 {"id": "s2", "capability": "draft_summary", "inputs": {"objective": "x", "facts": "$s1.summary"}}]})
    llm = MockLLM([bad, good])
    trace = DecisionTrace("t1")
    plan = LLMPlanner(llm, catalog).plan("Summarise revenue per year.", trace)
    assert plan is not None and plan.source.startswith("llm")
    assert "revenue_by_yer" in llm.calls[1][1]["content"]           # rejection fed back
    assert [e.type for e in trace.events] == ["plan_proposed", "plan_rejected", "plan_proposed"]

    always_bad = MockLLM([bad])
    trace2 = DecisionTrace("t2")
    cascade = PlannerCascade([LLMPlanner(always_bad, catalog), build_heuristic_planner(catalog)])
    plan = cascade.plan("Summarise revenue per year.", trace2)
    assert plan.source == "heuristic"
    assert trace2.of_type("planner_fallback")


def test_policy_memory_only_serves_approved_rules(tmp_path):
    mem = PolicyMemory(str(tmp_path / "rules.json"))
    r = mem.add("Always include the method in forecasts.")
    assert mem.render() == ""                     # draft: not injected
    mem.approve(r.id)
    assert "Always include the method" in mem.render()
    reloaded = PolicyMemory(str(tmp_path / "rules.json"))
    assert reloaded.approved()[0].id == r.id


# ---------------------------------------------------------------------------
# Runtime, capabilities, approval gate
# ---------------------------------------------------------------------------
def test_capabilities_are_deterministic_over_chinook():
    series = forecast_next_year({"series": [{"year": 2009, "revenue": 100}, {"year": 2010, "revenue": 110},
                                            {"year": 2011, "revenue": 120}]})
    assert series["year"] == 2012 and series["forecast"] == 130.0 and series["slope"] == 10.0
    hit = search_policies({"query": "refund above 200"})
    assert hit["passages"] and "finance controller" in hit["summary"]


def test_runtime_runs_waves_and_uses_fallback(catalog):
    async def go():
        trace = DecisionTrace("r1")
        adapters = default_adapters()
        calls = {"n": 0}

        def flaky(inputs):
            calls["n"] += 1
            raise RuntimeError("boom")
        adapters["revenue_by_country"] = flaky
        cat = Catalog([c if c.name != "revenue_by_country" else type(c)(**{**c.__dict__, "fallback": "customer_count_by_country"})
                       for c in catalog])
        rt = Runtime(cat, adapters, trace)
        plan = Plan("x", [PlanStep("s1", "revenue_by_country", {"top_n": 3}),
                          PlanStep("s2", "draft_summary", {"objective": "x", "facts": "$s1.summary"})])
        state = await rt.run(plan)
        return trace, state
    trace, state = asyncio.run(go())
    assert state.status == "completed" and state.fallbacks_used == 1
    assert "customers" in state.outputs["s1"]["summary"].lower()
    assert [e.type for e in trace.events][:7] == ["wave_started", "tool_call_allowed", "step_started", "step_failed",
                                                  "step_fallback", "tool_call_allowed", "step_finished"]


def test_runtime_pauses_at_approval_gate_and_resumes(catalog):
    async def go():
        trace = DecisionTrace("r2")
        rt = Runtime(catalog, default_adapters(), trace)
        plan = Plan("x", [PlanStep("s1", "send_report", {"recipient": "qa@example.com", "text": "hello"})])
        state = RunState()
        with pytest.raises(ApprovalRequired) as gate:
            await rt.run(plan, state)
        assert gate.value.step.id == "s1" and state.status == "awaiting_approval"
        state.approvals.add("s1")
        state = await rt.run(plan, state)
        return trace, state
    trace, state = asyncio.run(go())
    assert state.status == "completed" and state.outputs["s1"]["delivered"] is True
    assert trace.of_type("approval_required")


# ---------------------------------------------------------------------------
# LangGraph orchestration with interrupt / resume
# ---------------------------------------------------------------------------
def test_graph_interrupts_for_approval_and_resumes_via_command(catalog, tmp_path):
    orch = Orchestrator(catalog, default_adapters(), PlannerCascade([build_heuristic_planner(catalog)]),
                        trace_dir=str(tmp_path))
    snap = asyncio.run(orch.start("Forecast next year's revenue and send it to finance@example.com."))
    assert snap["status"] == "awaiting_approval"
    assert snap["pending_approval"]["capability"] == "send_report"
    assert set(snap["run_state"]["completed"]) == {"s1", "s2", "s3"}   # everything before the gate already ran

    resumed = asyncio.run(orch.resume(snap["run_id"], approved=True, by="tester"))
    assert resumed["status"] == "completed"
    assert "forecast" in resumed["answer"].lower()
    types = [e.type for e in orch.trace_for(snap["run_id"]).events]
    assert "approval_required" in types and "approval_granted" in types and types[-1] == "run_finished"
    assert (tmp_path / f"{snap['run_id']}.jsonl").exists()


def test_graph_denial_stops_the_run(catalog):
    orch = Orchestrator(catalog, default_adapters(), PlannerCascade([build_heuristic_planner(catalog)]))
    snap = asyncio.run(orch.start("Send the top 5 countries by revenue to sales@example.com."))
    assert snap["status"] == "awaiting_approval"
    denied = asyncio.run(orch.resume(snap["run_id"], approved=False))
    assert denied["status"] == "denied"
    assert "did not complete" in denied["answer"]


def test_audit_runs_every_objective_with_the_heuristic_planner(catalog):
    orch = Orchestrator(catalog, default_adapters(), PlannerCascade([build_heuristic_planner(catalog)]))
    objectives = json.loads((ROOT / "audit" / "objectives.json").read_text(encoding="utf-8"))
    report = asyncio.run(run_audit(orch, objectives, auto_approve=True))
    s = report.summary()
    # 19, not 20: "Send ... to the sales director" names no address in an allowed domain, so since the
    # catalog gained recipient constraints that objective is rejected at planning instead of delivered.
    assert s["objectives"] == 20 and s["answered"] == 19 and s["planner_fallbacks"] == 0
    assert s["approvals_raised"] == 2
    rejected = [c for c in report.cases if c.status == "failed"]
    assert [c.objective for c in rejected] == ["Send the top 5 countries by revenue to the sales director."]
    assert "| Answered | 19 / 20 |" in report.to_markdown()


def test_mine_rules_turns_fallbacks_into_draft_suggestions():
    t = DecisionTrace("m")
    t.emit("step_fallback", capability="a", fallback="b", objective="do the thing")
    t.emit("plan_rejected", planner="llm", errors=["Step s1 uses unknown capability 'x'."])
    rules = mine_rules(t.events)
    assert any("plan 'b' directly" in r for r in rules) and any("unknown capability" in r for r in rules)
