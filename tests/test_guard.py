"""Bounded tool execution: constraints, the shared run budget, effect classes, and the trace."""
import asyncio
import pathlib
import threading

import pytest

from orchestrator.capabilities import default_adapters
from orchestrator.catalog import Capability, Catalog
from orchestrator.graph import Orchestrator
from orchestrator.guard import RunBudget, check_constraints
from orchestrator.heuristics import build_heuristic_planner
from orchestrator.plan import Plan, PlanStep, parse_plan, validate_plan
from orchestrator.planner import PlannerCascade
from orchestrator.runtime import RunState, Runtime
from orchestrator.trace import DecisionTrace

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def catalog():
    return Catalog.load(str(ROOT / "capabilities.json"))


def _with(catalog: Catalog, name: str, **changes) -> Catalog:
    return Catalog([c if c.name != name else Capability(**{**c.__dict__, **changes}) for c in catalog])


# ---------------------------------------------------------------------------
# RunBudget
# ---------------------------------------------------------------------------
def test_budget_reserve_release_and_unlimited():
    b = RunBudget(2)
    assert b.reserve() and b.reserve() and not b.reserve()
    assert b.remaining == 0 and b.denied == 1
    b.release()
    assert b.reserve() and b.used == 2
    assert RunBudget(None).reserve(1000) and RunBudget(None).remaining is None
    with pytest.raises(ValueError):
        RunBudget(-1)


def test_children_cannot_spend_more_than_their_parent_between_them():
    parent = RunBudget(10)
    a, b = parent.child(10, "a"), parent.child(10, "b")
    assert all(a.reserve() for _ in range(6))
    assert all(b.reserve() for _ in range(4))
    assert not b.reserve() and not a.reserve()          # parent is full although each child has room
    assert parent.used == 10 and a.used == 6 and b.used == 4
    grandchild = a.child(3, "aa")
    assert not grandchild.reserve()                      # every ancestor is checked, not only the nearest
    parent.release(1)                                   # release at the parent frees the tree
    assert grandchild.reserve() and parent.used == 10 and a.used == 7 and grandchild.used == 1


def test_concurrent_reservations_never_exceed_the_limit():
    limit = 50
    budget = RunBudget(limit)
    wins = []

    def worker():
        for _ in range(40):
            if budget.reserve():
                wins.append(1)
    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wins) == limit and budget.used == limit and budget.denied == 8 * 40 - limit

    async def race():
        shared = RunBudget(5)
        results = await asyncio.gather(*(asyncio.to_thread(shared.reserve) for _ in range(20)))
        return sum(results), shared.used
    assert asyncio.run(race()) == (5, 5)


# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------
def test_check_constraints_reports_every_violation_with_a_reason():
    spec = {"top_n": {"min": 1, "max": 50}, "recipient": {"allowed_domains": ["example.com"]},
            "mode": {"allowed": ["fast", "full"]}, "code": {"pattern": "[A-Z]{3}"}, "text": {"max_length": 5}}
    ok = check_constraints(spec, {"top_n": 5, "recipient": "Finance@Example.com", "mode": "fast", "code": "ABC", "text": "hi"})
    assert ok == []
    bad = check_constraints(spec, {"top_n": 500, "recipient": "the sales director", "mode": "slow", "code": "abc", "text": "too long"})
    assert len(bad) == 5
    assert "maximum is 50" in bad[0] and "not an address in an allowed domain" in bad[1]
    assert check_constraints(spec, {"top_n": "$s1.count"}) == []       # unresolved references are the runtime's job
    assert check_constraints(spec, {"top_n": "many"}) == ["Input 'top_n' must be a number, got 'many'."]


def test_validator_applies_catalog_constraints_to_literal_arguments(catalog):
    plan = Plan("x", [PlanStep("s1", "revenue_by_country", {"top_n": 500}),
                      PlanStep("s2", "send_report", {"recipient": "someone@evil-example.org", "text": "hi"})])
    errors = validate_plan(plan, catalog)
    assert any("s1 (revenue_by_country): Input 'top_n' is 500; the maximum is 50." in e for e in errors)
    assert any("s2 (send_report)" in e and "allowed domain" in e for e in errors)


def test_catalog_rejects_misconfigured_effects_and_constraints(catalog):
    base = {"name": "x", "description": "d", "inputs": {"a": "string"}}
    with pytest.raises(ValueError, match="irreversible and must therefore require approval"):
        Catalog([Capability(**base, effect="irreversible")])
    with pytest.raises(ValueError, match="names no compensation"):
        Catalog([Capability(**base, effect="compensable")])
    with pytest.raises(ValueError, match="unknown capability 'undo'"):
        Catalog([Capability(**base, effect="compensable", compensation="undo")])
    with pytest.raises(ValueError, match="has effect 'maybe'"):
        Catalog([Capability(**base, effect="maybe")])
    with pytest.raises(ValueError, match="constrains input 'b' which it does not declare"):
        Catalog([Capability(**base, constraints={"b": {"max": 1}})])
    with pytest.raises(ValueError, match="unknown constraint keys"):
        Catalog([Capability(**base, constraints={"a": {"maximum": 1}})])
    ok = Catalog([Capability(**base, effect="compensable", compensation="undo"),
                  Capability(name="undo", description="compensates x")])
    assert ok.get("x").compensation == "undo"
    assert "effect=compensable, compensated by undo" in ok.render()


def test_effect_class_comes_from_the_catalog_not_the_plan(catalog):
    text = '{"steps": [{"id": "s1", "capability": "send_report", "effect": "reversible", "requires_approval": false, ' \
           '"inputs": {"recipient": "qa@example.com", "text": "hi"}}]}'
    plan = parse_plan(text, "obj")
    assert not hasattr(plan.steps[0], "effect") and validate_plan(plan, catalog) == []

    async def go():
        trace = DecisionTrace("effect")
        rt = Runtime(catalog, default_adapters(), trace)
        with pytest.raises(Exception, match="requires human approval"):
            await rt.run(plan, RunState())
        return trace
    trace = asyncio.run(go())
    assert trace.of_type("approval_required")[0].data["effect"] == "irreversible"


# ---------------------------------------------------------------------------
# Runtime: denial, budget sharing, fallback accounting, trace
# ---------------------------------------------------------------------------
def test_runtime_denies_a_resolved_argument_that_violates_a_constraint(catalog):
    cat = _with(catalog, "draft_summary", constraints={"facts": {"max_length": 10}})
    plan = Plan("x", [PlanStep("s1", "revenue_by_year", {}),
                      PlanStep("s2", "draft_summary", {"objective": "x", "facts": "$s1.summary"})])
    assert validate_plan(plan, cat) == []                # the reference cannot be checked before it resolves

    async def go():
        trace = DecisionTrace("deny")
        state = await Runtime(cat, default_adapters(), trace).run(plan)
        return trace, state
    trace, state = asyncio.run(go())
    assert state.status == "failed" and state.denied == {"s2": "constraint"}
    denied = trace.of_type("tool_call_denied")
    assert len(denied) == 1 and denied[0].data["reason"] == "constraint" and "maximum is 10" in denied[0].data["violations"][0]
    assert [e.data["capability"] for e in trace.of_type("tool_call_allowed")] == ["revenue_by_year"]
    assert not trace.of_type("step_fallback")            # a denial is not a failure to retry


def test_siblings_share_one_run_budget_and_dependants_are_blocked(catalog):
    plan = Plan("x", [PlanStep("s1", "revenue_by_year", {}),
                      PlanStep("s2", "top_genres_by_tracks_sold", {"top_n": 3}),
                      PlanStep("s3", "draft_summary", {"objective": "x", "facts": "$s1.summary"})])
    assert validate_plan(plan, catalog) == []

    async def go():
        trace = DecisionTrace("budget")
        state = await Runtime(catalog, default_adapters(), trace).run(plan, RunState(call_limit=1))
        return trace, state
    trace, state = asyncio.run(go())
    assert state.status == "failed" and len(state.completed) == 1 and state.calls_used == 1
    winner = next(iter(state.completed))
    loser = "s2" if winner == "s1" else "s1"
    assert state.denied[loser] == "budget"
    if winner == "s1":      # s3's dependency completed, so s3 reached the guard and was denied for budget
        assert state.denied["s3"] == "budget" and len(state.denied) == 2
    else:                   # s1 was denied, so s3 never reached the guard
        assert state.failed["s3"] == "blocked by a failed dependency" and len(state.denied) == 1
    types = [e.type for e in trace.events]
    assert types.count("tool_call_allowed") == 1 and types.count("tool_call_denied") == len(state.denied)
    assert types.count("budget_exhausted") == 1
    assert trace.of_type("run_failed")[0].data["budget"] == {"scope": "run", "limit": 1, "used": 1, "remaining": 0}


def test_fallback_calls_consume_budget_too(catalog):
    adapters = default_adapters()

    def flaky(inputs):
        raise RuntimeError("boom")
    adapters["revenue_by_country"] = flaky
    cat = _with(catalog, "revenue_by_country", fallback="customer_count_by_country")
    plan = Plan("x", [PlanStep("s1", "revenue_by_country", {"top_n": 3})])

    async def go(limit):
        trace = DecisionTrace(f"fb{limit}")
        state = await Runtime(cat, adapters, trace).run(plan, RunState(call_limit=limit))
        return trace, state
    trace, state = asyncio.run(go(2))
    assert state.status == "completed" and state.calls_used == 2 and state.fallbacks_used == 1
    assert [e.data["via_fallback"] for e in trace.of_type("tool_call_allowed")] == [False, True]

    trace, state = asyncio.run(go(1))
    assert state.status == "failed" and state.denied == {"s1": "budget"}
    assert "fallback denied (budget)" in state.failed["s1"]


def test_budget_survives_the_approval_interrupt(catalog):
    orch = Orchestrator(catalog, default_adapters(), PlannerCascade([build_heuristic_planner(catalog)]), call_limit=4)
    snap = asyncio.run(orch.start("Forecast next year's revenue and send it to finance@example.com."))
    assert snap["status"] == "awaiting_approval" and snap["run_state"]["calls_used"] == 3
    resumed = asyncio.run(orch.resume(snap["run_id"], approved=True))
    assert resumed["status"] == "completed" and resumed["run_state"]["calls_used"] == 4

    tight = Orchestrator(catalog, default_adapters(), PlannerCascade([build_heuristic_planner(catalog)]), call_limit=3)
    snap = asyncio.run(tight.start("Forecast next year's revenue and send it to finance@example.com."))
    resumed = asyncio.run(tight.resume(snap["run_id"], approved=True))
    assert resumed["status"] == "failed" and resumed["run_state"]["denied"] == {"s4": "budget"}
    assert "did not complete" in resumed["answer"]


def test_planner_output_addressed_outside_the_domain_never_runs(catalog):
    orch = Orchestrator(catalog, default_adapters(), PlannerCascade([build_heuristic_planner(catalog)]))
    snap = asyncio.run(orch.start("Forecast next year's revenue and send it to finance@evil-example.org."))
    assert snap["status"] == "failed" and snap["plan"] is None
    trace = orch.trace_for(snap["run_id"])
    proposed = trace.of_type("plan_proposed")[0].data["errors"]
    assert any("allowed domain" in e for e in proposed)
    assert not trace.of_type("step_started") and not trace.of_type("tool_call_allowed")
