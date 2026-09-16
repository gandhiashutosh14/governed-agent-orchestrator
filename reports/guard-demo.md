# Guard demo: bounded tool execution

Generated 2026-09-16 17:56 UTC at revision d5bd49b with `orchestrator demo-guard --report reports/guard-demo.md`. Planner: deterministic heuristic rules (scenarios 1 and 2) and a plan written in `orchestrator/demo.py` (scenario 3). No language model was involved.

## 1. allowed: Forecast next year's revenue and send it to finance@example.com.

Status: `completed`

Recipient is in the allowed domain (example.com); the irreversible send waits for approval, the demo approves it, and every call is journaled with its effect class and budget use. `approval_required` appears twice because LangGraph re-executes the node on resume and the runtime reaches the gate again before the recorded decision is applied; the second one is the resume, not a second ask.

| seq | event | detail |
|---|---|---|
| 2 | plan_proposed | heuristic: revenue_by_year -> forecast_next_year -> draft_summary -> send_report |
| 3 | plan_accepted | heuristic: revenue_by_year -> forecast_next_year -> draft_summary -> send_report |
| 5 | tool_call_allowed | revenue_by_year effect=reversible budget 1/None |
| 7 | step_finished | revenue_by_year in 1 ms |
| 9 | tool_call_allowed | forecast_next_year effect=reversible budget 2/None |
| 11 | step_finished | forecast_next_year in 0 ms |
| 13 | tool_call_allowed | draft_summary effect=reversible budget 3/None |
| 15 | step_finished | draft_summary in 0 ms |
| 17 | approval_required | send_report recipient=finance@example.com effect=irreversible |
| 19 | approval_required | send_report recipient=finance@example.com effect=irreversible |
| 20 | approval_granted | {"step": "s4", "by": "demo"} |
| 22 | tool_call_allowed | send_report effect=irreversible budget 4/None |
| 24 | step_finished | send_report in 0 ms |
| 25 | run_finished | completed=['s1', 's2', 's3', 's4'] failed=[] budget={"scope": "run", "limit": null, "used": 4, "remaining": null} |

## 2. denied: Forecast next year's revenue and send it to finance@evil-example.org.

Status: `failed`

Recipient is outside the allowed domain. The validator applies the catalog constraint to the literal argument, so the plan is rejected before any tool runs; the run ends with no valid plan.

| seq | event | detail |
|---|---|---|
| 2 | plan_proposed | heuristic: revenue_by_year -> forecast_next_year -> draft_summary -> send_report; errors: Step s4 (send_report): Input 'recipient' is 'finance@evil-example.org', which is not an address in an allowed domain (example.com). |
| 3 | run_failed | completed=None failed=[] budget=null |

## 3. exhausted: Two independent lookups, then a summary, under a one-call budget.

Status: `failed`

s1 and s2 are siblings in the first wave and race for one shared unit of budget. Which sibling wins is not fixed; that exactly one wins is. s3 depends on s1: if s1 won it reaches the guard and is denied for budget, if s1 lost it is blocked before the guard. In this run: s1: completed; s2: denied (budget): run call budget of 1 exhausted before step s2 (top_genres_by_tracks_sold); s3: denied (budget): run call budget of 1 exhausted before step s3 (draft_summary).

| seq | event | detail |
|---|---|---|
| 2 | tool_call_allowed | revenue_by_year effect=reversible budget 1/1 |
| 4 | tool_call_denied | top_genres_by_tracks_sold reason=budget: {"scope": "run", "limit": 1, "used": 1, "remaining": 0} |
| 5 | budget_exhausted | {"scope": "run", "limit": 1, "used": 1, "remaining": 0} |
| 6 | step_finished | revenue_by_year in 0 ms |
| 8 | tool_call_denied | draft_summary reason=budget: {"scope": "run", "limit": 1, "used": 1, "remaining": 0} |
| 9 | run_failed | completed=['s1'] failed=['s2', 's3'] budget={"scope": "run", "limit": 1, "used": 1, "remaining": 0} |
