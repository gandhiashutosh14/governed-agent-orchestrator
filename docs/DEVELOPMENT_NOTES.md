# Development notes

How this project was built. It was written from scratch, so there is no earlier history to
reconstruct.

## Why this project exists

The author's professional work involves governed multi-agent analytics systems whose code cannot
be published. This repository re-implements the *patterns* from that kind of work on
public data (the Chinook sample database and four short policy documents written for the demo):
a capability catalog with typed contracts, plan validation, an LLM-then-heuristic planner cascade,
parallel-wave execution with fallbacks, a DecisionTrace journal, governed policy memory, and a
LangGraph runtime that pauses for human approval. No employer code, prompts, schemas or data were
used or consulted while writing it.

## First build, 2026-09-16

### Planning

- Chosen scope: the smallest system that demonstrates every named pattern end to end, with a
  deterministic demo domain (music-store sales analytics) so the only non-determinism is the
  optional LLM planner.
- Decision: the runtime must be idempotent over its own state, because LangGraph re-executes a
  node from the top after an interrupt. Completed steps are skipped on re-entry.
- Decision: the only side-effecting capability (`send_report`) is the only one that requires
  approval, so the approval gate is exercised by any objective that asks for delivery.

### Iterations

1. Pure modules first: `catalog.py`, `plan.py` (parser, validator, dependency waves), `trace.py`,
   `policy.py`. Then `planner.py` (LLM planner with one repair round, heuristic planner, cascade),
   `runtime.py` (waves via `asyncio.gather`, per-step timeouts from the capability budget, fallback
   capability, approval gate), and the demo `capabilities.py`.
2. `graph.py`: a three-node LangGraph (`plan → execute → synthesize`) compiled with a checkpointer,
   `interrupt()` inside the execute node when the runtime raises `ApprovalRequired`, resumed with
   `Command(resume=...)`.
3. `api.py`: FastAPI routes for runs, an SSE stream that replays the DecisionTrace and then follows
   it live, approval, capabilities and policy rules. `audit.py` and `cli.py` for batch runs.
4. 15 tests, driving everything with the heuristic planner and scripted models.

### Debugging (all found by the tests or the real runs)

- The recipient regex for "send ... to X" clipped `finance@example.com` to `finance@example` and
  did not match "send the top 5 countries by revenue to the sales director". Rewritten to allow
  words between the verb and "to" and to stop only at a sentence-ending period.
- The policy rule was ordered after the forecast rule, so "what must a forecast state before it can
  be circulated?" was planned as a forecast. Policy now matches first; its keywords were widened so
  the data-retention objective plans at all (it had been the one failure in a 20-objective audit).
- LangGraph discards a node's partial updates when it interrupts, so an approved run re-executed
  every step before the gate. The runtime state is now parked in the orchestrator across the
  interrupt and picked up on resume, and the snapshot returned while awaiting approval reports the
  steps already completed.
- The 1.5B local planner once returned `inputs` as a string; the parser passed it to `dict()` and
  crashed the audit. It is now a parse error the planner is asked to repair, with a test.

### Verification

| Check | Result |
|---|---|
| `pytest -q` | 15 passed |
| Audit, heuristic planner only, 20 objectives | 20/20 valid plans, 20/20 answered, 3 approval gates raised and auto-approved, median 9 ms |
| API smoke test (uvicorn) | `POST /runs` returned `awaiting_approval` with steps s1..s3 already completed; `GET /runs/{id}/events` replayed 12 trace events; `POST /approve` completed the run; a second approve returned 409 |
| Audit with `hf:Qwen/Qwen2.5-Coder-1.5B-Instruct` as first planner | see README (numbers recorded from the run) |

### What is and is not claimed

The demo capabilities are deterministic SQL over Chinook plus a least-squares trend; there is no
retrieval model and no LLM in the capabilities themselves. The LLM is used only as a planner, and
its plans are always validated before execution. The audit numbers describe this repository on this
machine, nothing else.

## Second increment, 2026-09-16: bounded tool execution

The second increment was the smallest verifiable addition to the existing runtime: a guard in
front of every tool call, with argument constraints and effect classes declared in the catalog,
and a call budget shared by the whole run.

### Decisions

- Constraints and effect classes live in `capabilities.json`, which the operator loads; the plan
  format has no such fields, so a planner cannot relabel an irreversible tool or widen a bound.
- The budget is one counter per run under one lock. Child scopes exist and are tested (two
  children of a ten-call budget cannot spend twenty between them) but the runtime has no
  sub-plans, so today the sharing that matters is between concurrent steps in a wave and between
  a step and its fallback call.
- A constraint or budget denial is not retried and not sent to the fallback; the plan is at fault.
- Literal arguments are checked by the validator so a bad plan never runs; arguments that resolve
  from earlier outputs are checked again by the runtime.

### Debugging (found by the tests)

- The sibling-budget test assumed the dependant step is always "blocked". It is blocked only when
  its dependency lost the race; when the dependency won, the dependant reaches the guard and is
  denied for budget. The test now asserts whichever of the two happened, and the demo report says
  which it saw.
- A run that failed at execution produced an empty answer, while a denied run produced a partial
  answer listing what did not complete. The synthesize node now skips only when there is no plan.
- Adding a recipient-domain constraint changed the audit: "Send the top 5 countries by revenue to
  the sales director" is rejected at planning because "the sales director" is not an address.
  The objective was kept, the audit report regenerated (19 / 20 answered, 2 gates), and the
  README explains the difference from the earlier 20 / 20.
- On resume, LangGraph re-executes the node, so a resumed run's trace holds `approval_required`
  twice. Pre-existing; now stated in the demo report rather than hidden.

### Verification

| Check | Result |
|---|---|
| `pytest -q` | 27 passed (15 existing, 12 new in `tests/test_guard.py`) |
| `orchestrator demo-guard --report reports/guard-demo.md` | three scenarios: allowed and delivered; rejected before execution; one of two siblings denied under a one-call budget |
| `orchestrator audit ...` | 19 / 20 answered, 2 approval gates, 0 fallbacks, median 6 ms |

### What is and is not claimed

The guard bounds arguments, call counts and effect classes. It does not track the provenance of
values, does not execute compensations, and was not evaluated against a model that tries to evade
it. The demo shows what the reference monitor does with the arguments it is given.
