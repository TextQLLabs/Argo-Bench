#!/usr/bin/env python3
"""Package Inspect logs for scoring against the held-out answer keys.

    python scripts/export_submission.py --log-dir logs --out submissions/my-run.jsonl.gz

One line per question run: the question, the model and its settings, what the run filed
through Mission Control, its token usage and time, and how it ended. The full transcripts
stay in the Inspect logs; send those too if you want the maintainers to audit a run.
"""

from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--log-dir", default="logs")
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()

    from inspect_ai.log import list_eval_logs, read_eval_log

    rows = 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.out, "wt", encoding="utf-8") as out:
        for info in list_eval_logs(args.log_dir):
            log = read_eval_log(info)
            if log.eval.task not in ("argo-bench", "argo_bench") or not log.samples:
                continue
            config = log.plan.config.model_dump(exclude_none=True) if log.plan else {}
            for sample in log.samples:
                row = {
                    "question_id": sample.id, "epoch": sample.epoch,
                    "model": log.eval.model, "generate_config": config,
                    "model_args": log.eval.model_args, "task_args": log.eval.task_args,
                    "rung": (log.eval.metadata or {}).get("rung", ""),
                    "inspect_version": log.eval.packages.get("inspect_ai", ""),
                    "log": Path(info.name).name,
                    "filings": (sample.store or {}).get("filings", []),
                    "model_usage": {k: v.model_dump(exclude_none=True)
                                    for k, v in (sample.model_usage or {}).items()},
                    "total_time": sample.total_time, "working_time": sample.working_time,
                    "limit": sample.limit.model_dump() if sample.limit else None,
                    "error": sample.error.message if sample.error else None,
                    "n_messages": len(sample.messages),
                }
                out.write(json.dumps(row, default=str) + "\n")
                rows += 1
    print(f"wrote {rows} run(s) to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
