"""Local-only dashboard and sanitized observability ingestion endpoint.

No internet-facing auth, not suitable for publicly exposed deployment.
"""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from harness import ROOT, OUT, Issue, build_demo
from retrieval import ContextRetriever, benchmark, fixture_corpus
from tiered_os import demonstration

LOCK = threading.Lock()
SIGNALS = OUT / "signals.jsonl"
VALID_SIGNAL = re.compile(r"^[a-zA-Z0-9_.:-]{1,64}$")


def read_signals():
    if not SIGNALS.exists():
        return []
    lines = SIGNALS.read_text(encoding="utf-8").splitlines()[-100:]
    return [json.loads(x) for x in lines if x.strip()]


def signal_issues():
    dedup = {}
    for s in read_signals():
        key = f"{s['service']}:{s['signal_type']}"
        dedup[key] = s
    issues = []
    for index, (key, s) in enumerate(sorted(dedup.items()), 201):
        known = {"api": ["src/api.py"], "auth": ["src/auth.py"], "store": ["src/store.py"]}
        paths = known.get(s["service"], [])
        issues.append(Issue(id=f"OBS-{index}", title=f"Investigate {s['signal_type']} in {s['service']} ({s['count']} occurrences)",
                            paths=paths, intent="observed:" + key, risk="normal", origin="Sanitized external telemetry",
                            acceptance=["Correlate anomaly with relevant component", "Independent review checks"], severity=s["severity"]))
    return issues


def ingest_signal(payload):
    # Whitelist, validate & avoid storing raw traces, PII, credentials, and body contents.
    permitted = {"service", "signal_type", "count", "severity"}
    if set(payload) - permitted:
        raise ValueError("Only service, signal_type, count, severity accepted; raw log data prohibited")
    service = payload.get("service", "")
    signal_type = payload.get("signal_type", "")
    count = payload.get("count", 0)
    severity = payload.get("severity", "warning")
    if not VALID_SIGNAL.fullmatch(service) or not VALID_SIGNAL.fullmatch(signal_type):
        raise ValueError("service/signal_type must be 1–64 safe characters")
    if not isinstance(count, int) or isinstance(count, bool) or not (1 <= count <= 1000000):
        raise ValueError("count must be an integer from 1 to 1,000,000")
    if severity not in {"info", "warning", "critical"}:
        raise ValueError("severity must be info, warning, or critical")
    event = {"service": service, "signal_type": signal_type, "count": count, "severity": severity}
    OUT.mkdir(exist_ok=True, parents=True)
    with SIGNALS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")
    return event


def load_data():
    try:
        return {"baseline": json.loads((OUT / "baseline.json").read_text()),
                "optimized": json.loads((OUT / "optimized.json").read_text()),
                "comparison": json.loads((OUT / "comparison.json").read_text()),
                "signals": read_signals(), "retrieval": benchmark()}
    except FileNotFoundError:
        build_demo(extra_issues=signal_issues())
        return load_data()


class Handler(BaseHTTPRequestHandler):
    def send(self, status, data, type="application/json; charset=utf-8"):
        raw = (json.dumps(data).encode("utf-8") if not isinstance(data, bytes) else data)
        self.send_response(status)
        self.send_header("Content-Type", type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/":
            self.send(200, (ROOT / "dashboard" / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/data":
            with LOCK:
                self.send(200, load_data())
        elif path == "/api/os/demo":
            # Public synthetic fixture; no provider credentials or agent actions.
            self.send(200, demonstration())
        else:
            self.send(404, {"error": "not found"})

    def do_POST(self):
        path = urlsplit(self.path).path
        if path not in {"/api/run", "/api/ingest", "/api/search"}:
            return self.send(404, {"error": "not found"})
        if int(self.headers.get("Content-Length", "0")) > 4096:
            return self.send(413, {"error": "payload too large"})
        try:
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if not isinstance(payload, dict):
                raise ValueError("body must be an object")
            with LOCK:
                if path == "/api/search":
                    query = payload.get("query", "")
                    tenant = payload.get("tenant", "demo")
                    budget = payload.get("budget_words", 130)
                    if not isinstance(query, str) or not 2 <= len(query) <= 240:
                        raise ValueError("query must contain 2..240 characters")
                    if tenant not in {"demo", "support"}:
                        raise ValueError("only built-in synthetic demo and support collections allowed")
                    if not isinstance(budget, int) or isinstance(budget, bool) or not 20 <= budget <= 500:
                        raise ValueError("budget_words must be integer 20..500")
                    retriever = ContextRetriever(fixture_corpus()[0])
                    result = retriever.retrieve(query, mode="adaptive", tenant=tenant, budget_words=budget)
                elif path == "/api/ingest":
                    received = ingest_signal(payload)
                    result = {"accepted": True, "sanitized_signal": received, "next": "rerun to triage"}
                else:
                    budget = payload.get("budget", 120000)
                    parallel = payload.get("parallel", 3)
                    inject = payload.get("inject_change", False)
                    if not isinstance(budget, int) or isinstance(budget, bool) or not 1000 <= budget <= 500000:
                        raise ValueError("budget must be integer 1000..500000")
                    if not isinstance(parallel, int) or isinstance(parallel, bool) or not 1 <= parallel <= 8:
                        raise ValueError("parallel must be integer 1..8")
                    if not isinstance(inject, bool):
                        raise ValueError("inject_change must be boolean")
                    build_demo(budget=budget, max_parallel=parallel, inject_change=inject,
                               extra_issues=signal_issues())
                    result = load_data()
            self.send(200, result)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self.send(400, {"error": str(exc)})
        except Exception as exc:
            self.send(500, {"error": "Internal error", "details": type(exc).__name__})


def main():
    build_demo(extra_issues=signal_issues())
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    print("Autonomous SDLC PoC: http://127.0.0.1:8765  (Ctrl+C to stop)")
    server.serve_forever()


if __name__ == "__main__":
    main()