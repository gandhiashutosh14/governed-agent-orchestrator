"""
Demo capabilities for a music-store sales analytics assistant over the public
Chinook database. Each adapter is a plain function (sync or async) taking the
resolved inputs and returning a dict whose keys match the catalog's declared
outputs. Everything here is deterministic; the only LLM in the system is the
planner (and, optionally, draft_summary).
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "chinook.sqlite"
POLICY_DIR = ROOT / "docs" / "policies"
OUTBOX = ROOT / "outbox"


def _query(sql: str, params: tuple = ()) -> List[tuple]:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
def revenue_by_year(inputs: Dict[str, Any]) -> Dict[str, Any]:
    rows = _query("SELECT strftime('%Y', InvoiceDate) AS y, ROUND(SUM(Total), 2) FROM Invoice GROUP BY y ORDER BY y")
    series = [{"year": int(y), "revenue": float(r)} for y, r in rows]
    return {"series": series,
            "summary": "; ".join(f"{p['year']}: {p['revenue']:.2f}" for p in series)}


def revenue_by_country(inputs: Dict[str, Any]) -> Dict[str, Any]:
    top_n = int(inputs.get("top_n") or 5)
    year = inputs.get("year")
    if year:
        rows = _query("SELECT BillingCountry, ROUND(SUM(Total), 2) FROM Invoice WHERE strftime('%Y', InvoiceDate) = ? "
                      "GROUP BY BillingCountry ORDER BY SUM(Total) DESC LIMIT ?", (str(year), top_n))
    else:
        rows = _query("SELECT BillingCountry, ROUND(SUM(Total), 2) FROM Invoice GROUP BY BillingCountry "
                      "ORDER BY SUM(Total) DESC LIMIT ?", (top_n,))
    table = [{"country": c, "revenue": float(r)} for c, r in rows]
    scope = f" in {year}" if year else ""
    return {"rows": table, "summary": f"Top {len(table)} countries by revenue{scope}: " +
            ", ".join(f"{t['country']} {t['revenue']:.2f}" for t in table)}


def top_genres_by_tracks_sold(inputs: Dict[str, Any]) -> Dict[str, Any]:
    top_n = int(inputs.get("top_n") or 5)
    rows = _query("SELECT g.Name, SUM(il.Quantity) FROM InvoiceLine il JOIN Track t ON t.TrackId = il.TrackId "
                  "JOIN Genre g ON g.GenreId = t.GenreId GROUP BY g.GenreId ORDER BY SUM(il.Quantity) DESC LIMIT ?", (top_n,))
    table = [{"genre": g, "tracks_sold": int(n)} for g, n in rows]
    return {"rows": table, "summary": f"Top {len(table)} genres by tracks sold: " +
            ", ".join(f"{t['genre']} {t['tracks_sold']}" for t in table)}


def customer_count_by_country(inputs: Dict[str, Any]) -> Dict[str, Any]:
    top_n = int(inputs.get("top_n") or 5)
    rows = _query("SELECT Country, COUNT(*) FROM Customer GROUP BY Country ORDER BY COUNT(*) DESC, Country LIMIT ?", (top_n,))
    table = [{"country": c, "customers": int(n)} for c, n in rows]
    return {"rows": table, "summary": f"Customers by country (top {len(table)}): " +
            ", ".join(f"{t['country']} {t['customers']}" for t in table)}


def forecast_next_year(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """Least-squares linear trend over a yearly series; deliberately simple and explainable."""
    series = inputs.get("series") or []
    if isinstance(series, str):
        series = json.loads(series)
    pts = [(int(p["year"]), float(p["revenue"])) for p in series]
    if len(pts) < 2:
        raise ValueError("forecast_next_year needs at least two yearly points")
    n = len(pts)
    mean_x = sum(x for x, _ in pts) / n
    mean_y = sum(y for _, y in pts) / n
    sxx = sum((x - mean_x) ** 2 for x, _ in pts)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in pts) / sxx if sxx else 0.0
    intercept = mean_y - slope * mean_x
    next_year = max(x for x, _ in pts) + 1
    forecast = round(intercept + slope * next_year, 2)
    return {"year": next_year, "forecast": forecast, "slope": round(slope, 2), "method": "least-squares linear trend",
            "summary": f"Linear-trend forecast for {next_year}: {forecast:.2f} (slope {slope:+.2f}/year over {n} years)"}


def search_policies(inputs: Dict[str, Any]) -> Dict[str, Any]:
    query = str(inputs.get("query") or "")
    terms = {w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2}
    passages: List[Dict[str, Any]] = []
    for path in sorted(POLICY_DIR.glob("*.md")):
        for para in [p.strip() for p in path.read_text(encoding="utf-8").split("\n\n") if p.strip()]:
            words = set(re.findall(r"[a-z0-9]+", para.lower()))
            score = len(terms & words)
            if score:
                passages.append({"doc": path.name, "text": para, "score": score})
    passages.sort(key=lambda p: -p["score"])
    top = passages[:3]
    return {"passages": top, "summary": " ".join(p["text"] for p in top) if top else "No matching policy text found."}


def draft_summary(inputs: Dict[str, Any], llm: Optional[Callable[[List[Dict[str, str]]], str]] = None) -> Dict[str, Any]:
    objective = str(inputs.get("objective") or "")
    facts = inputs.get("facts")
    if isinstance(facts, (dict, list)):
        facts_text = json.dumps(facts, default=str)
    else:
        facts_text = str(facts or "")
    if llm:
        text = llm([{"role": "system", "content": "Write a concise, factual business summary using only the facts given."},
                    {"role": "user", "content": f"Objective: {objective}\nFacts: {facts_text}"}])
        return {"text": text.strip(), "method": "llm"}
    return {"text": f"{objective.strip().rstrip('.')}. Findings: {facts_text}", "method": "template"}


def send_report(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """The one side-effecting capability. Guarded by requires_approval in the catalog."""
    OUTBOX.mkdir(exist_ok=True)
    recipient = str(inputs.get("recipient") or "unknown")
    text = str(inputs.get("text") or "")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = OUTBOX / f"{stamp}_{re.sub(r'[^a-z0-9]+', '_', recipient.lower())}.txt"
    path.write_text(f"To: {recipient}\n\n{text}\n", encoding="utf-8")
    return {"delivered": True, "path": str(path)}


def default_adapters(summary_llm: Optional[Callable] = None) -> Dict[str, Callable]:
    return {
        "revenue_by_year": revenue_by_year,
        "revenue_by_country": revenue_by_country,
        "top_genres_by_tracks_sold": top_genres_by_tracks_sold,
        "customer_count_by_country": customer_count_by_country,
        "forecast_next_year": forecast_next_year,
        "search_policies": search_policies,
        "draft_summary": lambda inputs: draft_summary(inputs, summary_llm),
        "send_report": send_report,
    }
