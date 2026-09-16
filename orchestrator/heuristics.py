"""
Deterministic planning rules for the demo domain. These are the floor of the
planner cascade: when no model is configured, or the model's plan is rejected,
these rules still produce a valid plan for the objectives the catalog covers.
"""
from __future__ import annotations

import re
from typing import List, Match

from .catalog import Catalog
from .plan import PlanStep
from .planner import HeuristicPlanner

# "send it to finance@example.com." / "email the summary to reporting@example.com" / "send ... to the sales director"
_RECIPIENT = re.compile(r"\b(?:send|email|deliver|mail)\b.*?\bto\s+([A-Za-z0-9 ._@-]+?)(?:\.(?=\s|$)|,|$|\s+and\b)", re.I | re.S)
_TOP_N = re.compile(r"\btop (\d+)\b", re.I)
_YEAR = re.compile(r"\b(20\d{2})\b")


def _top_n(objective: str, default: int = 5) -> int:
    m = _TOP_N.search(objective)
    return int(m.group(1)) if m else default


def _with_summary_and_delivery(objective: str, steps: List[PlanStep], facts_refs: List[str]) -> List[PlanStep]:
    facts = " | ".join(facts_refs) if len(facts_refs) == 1 else facts_refs[0]
    steps.append(PlanStep(id=f"s{len(steps) + 1}", capability="draft_summary",
                          inputs={"objective": objective, "facts": facts}, reason="summarise gathered facts"))
    m = _RECIPIENT.search(objective)
    if m:
        steps.append(PlanStep(id=f"s{len(steps) + 1}", capability="send_report",
                              inputs={"recipient": m.group(1).strip(), "text": f"$s{len(steps)}.text"},
                              reason="objective asks for delivery; needs approval"))
    return steps


def build_heuristic_planner(catalog: Catalog) -> HeuristicPlanner:
    hp = HeuristicPlanner(catalog)

    def forecast(objective: str, m: Match) -> List[PlanStep]:
        steps = [PlanStep(id="s1", capability="revenue_by_year", inputs={}, reason="need the yearly series"),
                 PlanStep(id="s2", capability="forecast_next_year", inputs={"series": "$s1.series"}, reason="fit trend")]
        return _with_summary_and_delivery(objective, steps, ["$s2.summary"])

    def by_country(objective: str, m: Match) -> List[PlanStep]:
        inputs = {"top_n": _top_n(objective)}
        y = _YEAR.search(objective)
        if y:
            inputs["year"] = int(y.group(1))
        steps = [PlanStep(id="s1", capability="revenue_by_country", inputs=inputs, reason="country revenue")]
        return _with_summary_and_delivery(objective, steps, ["$s1.summary"])

    def genres(objective: str, m: Match) -> List[PlanStep]:
        steps = [PlanStep(id="s1", capability="top_genres_by_tracks_sold", inputs={"top_n": _top_n(objective)}, reason="genre ranking")]
        return _with_summary_and_delivery(objective, steps, ["$s1.summary"])

    def customers(objective: str, m: Match) -> List[PlanStep]:
        steps = [PlanStep(id="s1", capability="customer_count_by_country", inputs={"top_n": _top_n(objective)}, reason="customer counts")]
        return _with_summary_and_delivery(objective, steps, ["$s1.summary"])

    def policy(objective: str, m: Match) -> List[PlanStep]:
        steps = [PlanStep(id="s1", capability="search_policies", inputs={"query": objective}, reason="policy lookup")]
        return _with_summary_and_delivery(objective, steps, ["$s1.summary"])

    def yearly(objective: str, m: Match) -> List[PlanStep]:
        steps = [PlanStep(id="s1", capability="revenue_by_year", inputs={}, reason="yearly revenue")]
        return _with_summary_and_delivery(objective, steps, ["$s1.summary"])

    # Order matters: policy questions can mention forecasts ("what must a forecast state..."), so they go first.
    hp.add_rule(r"polic(y|ies)|discount|refund|retention|retain|how long do we keep|allowed|rule|circulated|on their own", policy)
    hp.add_rule(r"forecast|next year|coming year|project(ed|ion)|predict", forecast)
    hp.add_rule(r"genre", genres)
    hp.add_rule(r"how many customers|customers? (per|by|in each) countr", customers)
    hp.add_rule(r"countr(y|ies)|market", by_country)
    hp.add_rule(r"per year|by year|yearly|each year|annual|over time|trend", yearly)
    return hp
