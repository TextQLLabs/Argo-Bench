#!/usr/bin/env python3
"""Run the questions on one or more model rungs of the paper's configuration.

    python scripts/run.py --list
    python scripts/run.py --rung opus-5.5-high
    python scripts/run.py --rung gpt-6-luna-low --rung gpt-6-luna-medium -T sandbox=docker
    python scripts/run.py --rung haiku-4.5 -T questions=smoke

A rung (``rungs.json``) is an Inspect model plus its generation settings: the reasoning
effort, and any provider arguments or environment it needs. Each rung writes its own Inspect
logs under ``logs/<rung>/``; ``inspect view --log-dir logs`` browses them and
``scripts/export_submission.py`` packages them. This is a thin wrapper: the same run is

    inspect eval argo_bench/task.py --model <model> --reasoning-effort <effort> [-M k=v]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
RUNGS = {r["key"]: r for r in json.loads((REPO / "rungs.json").read_text(encoding="utf-8"))}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--rung", action="append", default=[], choices=sorted(RUNGS), metavar="RUNG")
    ap.add_argument("--all", action="store_true", help="every rung of the paper, in order")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("-T", action="append", default=[], metavar="NAME=VALUE",
                    help="task option (see argo_bench/task.py), repeatable")
    ap.add_argument("--limit", type=int, default=None, help="first N questions only")
    ap.add_argument("--epochs", type=int, default=1)
    ap.add_argument("--max-samples", type=int, default=None,
                    help="questions in flight at once (Inspect's default otherwise)")
    ap.add_argument("--log-dir", default=str(REPO / "logs"))
    args = ap.parse_args()

    if args.list:
        for rung in RUNGS.values():
            print(f"{rung['key']:28s} {rung['model']:52s} {json.dumps(rung['config'])}")
        return 0
    chosen = list(RUNGS) if args.all else args.rung
    if not chosen:
        ap.error("name a --rung (or --all; --list shows them)")

    from dotenv import load_dotenv
    from inspect_ai import eval as inspect_eval

    load_dotenv(REPO / ".env")
    task_args = dict(pair.split("=", 1) for pair in args.T)
    ok = True
    for key in chosen:
        rung = RUNGS[key]
        for name, value in rung.get("env", {}).items():
            os.environ.setdefault(name, value)
        logs = inspect_eval(str(REPO / "argo_bench" / "task.py"), model=rung["model"],
                            model_args=rung.get("model_args") or {}, task_args=task_args,
                            limit=args.limit, epochs=args.epochs, max_samples=args.max_samples,
                            log_dir=str(Path(args.log_dir) / key), fail_on_error=False,
                            tags=[key], metadata={"rung": key}, **rung["config"])
        ok = ok and all(log.status == "success" for log in logs)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
