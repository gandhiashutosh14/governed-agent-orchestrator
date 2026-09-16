"""
CLI:
  python -m orchestrator.cli run "Forecast next year's revenue and send it to finance@example.com" [--llm hf:...] [--approve]
  python -m orchestrator.cli audit --objectives audit/objectives.json --report reports/audit.md [--llm hf:...]
  python -m orchestrator.cli serve [--port 8000]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Optional, Sequence

from .api import build_orchestrator
from .audit import run_audit, save_audit


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="orchestrator")
    sub = p.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--llm", default="none", help="none | mock | hf:<model-id> | openai:<model>")
    common.add_argument("--trace-dir", default="traces")

    r = sub.add_parser("run", parents=[common])
    r.add_argument("objective")
    r.add_argument("--approve", action="store_true", help="auto-approve any approval gate")

    a = sub.add_parser("audit", parents=[common])
    a.add_argument("--objectives", required=True)
    a.add_argument("--report", required=True)
    a.add_argument("--json", default=None)
    a.add_argument("--no-auto-approve", action="store_true")

    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--llm", default="none")

    args = p.parse_args(argv)

    if args.cmd == "serve":
        import os
        import uvicorn
        os.environ["ORCH_LLM"] = args.llm
        uvicorn.run("orchestrator.api:app", host="127.0.0.1", port=args.port)
        return 0

    orch, _, _ = build_orchestrator(args.llm, trace_dir=args.trace_dir)

    if args.cmd == "run":
        async def go():
            snap = await orch.start(args.objective)
            while snap["status"] == "awaiting_approval" and args.approve:
                snap = await orch.resume(snap["run_id"], True, by="cli")
            return snap
        snap = asyncio.run(go())
        print(json.dumps(snap, indent=2, default=str))
        return 0 if snap["status"] in ("completed", "awaiting_approval") else 1

    objectives = json.loads(open(args.objectives, encoding="utf-8").read())
    report = asyncio.run(run_audit(orch, objectives, auto_approve=not args.no_auto_approve))
    save_audit(report, args.report, args.json)
    print(json.dumps(report.summary(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
