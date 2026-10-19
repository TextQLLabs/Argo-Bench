#!/usr/bin/env python3
"""Grade finished runs with the Argo-Bench grading service, and print the table.

    export ARGO_GRADER_KEY=argo_...
    .venv/bin/python scripts/grade_remote.py --log-dir logs --out grades.csv
    .venv/bin/python scripts/grade_remote.py --submission submissions/my-run.jsonl.gz

Every argo-bench sample in the Inspect logs (or every line of a file written by
``export_submission.py``) is sent to the service with its filings; the reward comes back
and is written to ``--out`` (one row per run), and the mean per model is printed. Runs the
service refuses are listed and left out of the means. The logs are not modified; to write
the scores into a log, use ``inspect score <log> --scorer argo_bench/remote.py@remote_grade
--action append --overwrite``.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import gzip
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from argo_bench import remote  # noqa: E402


def from_logs(log_dir: str) -> list[dict]:
    from inspect_ai.log import list_eval_logs, read_eval_log

    runs = []
    for info in list_eval_logs(log_dir):
        log = read_eval_log(info)
        if log.eval.task not in ("argo-bench", "argo_bench") or not log.samples:
            continue
        for sample in log.samples:
            runs.append({"question_id": str(sample.id), "epoch": sample.epoch,
                         "version": (sample.metadata or {}).get("version", ""),
                         "model": log.eval.model, "run": Path(info.name).name,
                         "filings": (sample.store or {}).get("filings", [])})
    return runs


def from_submission(path: Path, versions: dict[str, str]) -> list[dict]:
    runs = []
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            runs.append({"question_id": row["question_id"], "epoch": row.get("epoch", 1),
                         # Files written before the version was exported: the current card's.
                         "version": row.get("version") or versions.get(row["question_id"], ""),
                         "model": row.get("model", ""), "run": row.get("log", ""),
                         "filings": row.get("filings", [])})
    return runs


async def grade_all(runs: list[dict], concurrency: int) -> None:
    gate = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(timeout=960) as client:
        async def one(run: dict) -> None:
            async with gate:
                try:
                    result = await remote.grade(run["question_id"], run["version"], run["filings"],
                                                model=run["model"],
                                                run=f"{run['run']} epoch {run['epoch']}",
                                                client=client)
                    run["reward"], run["grade_id"] = result["reward"], result["grade_id"]
                except remote.GraderRefused as exc:
                    run["error"] = str(exc)
                done = sum(1 for r in runs if "reward" in r or "error" in r)
                print(f"\r{done}/{len(runs)} graded", end="", file=sys.stderr, flush=True)
        await asyncio.gather(*(one(r) for r in runs))
    print(file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--log-dir")
    source.add_argument("--submission", type=Path)
    ap.add_argument("--out", type=Path, default=Path("grades.csv"))
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()
    if not remote.grader_key():
        raise SystemExit(f"set {remote.KEY_ENV} to your API key")

    versions = {c["id"]: c["version"] for c in map(json.loads,
                (REPO / "tasks/final.jsonl").read_text(encoding="utf-8").splitlines()) if c}
    runs = from_logs(args.log_dir) if args.log_dir else from_submission(args.submission, versions)
    runs = [r for r in runs if not r["question_id"].startswith("smoke-")]
    if not runs:
        raise SystemExit("no argo-bench runs found")
    asyncio.run(grade_all(runs, args.concurrency))

    with args.out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, ["model", "question_id", "epoch", "reward", "grade_id",
                                     "run", "error"], extrasaction="ignore")
        writer.writeheader()
        writer.writerows(sorted(runs, key=lambda r: (r["model"], r["question_id"], r["epoch"])))
    by_model: dict[str, list[float]] = defaultdict(list)
    for run in runs:
        if "reward" in run:
            by_model[run["model"]].append(run["reward"])
    print(f"{'model':48s} {'runs':>5s} {'mean':>7s} {'solved':>7s}")
    for model, rewards in sorted(by_model.items()):
        solved = sum(r >= 0.95 for r in rewards) / len(rewards)
        print(f"{model:48s} {len(rewards):5d} {statistics.mean(rewards):7.3f} {solved:7.1%}")
    refused = [r for r in runs if "error" in r]
    for run in refused[:10]:
        print(f"refused: {run['error']}", file=sys.stderr)
    print(f"wrote {args.out} ({len(runs) - len(refused)} graded, {len(refused)} refused)")
    return 1 if refused else 0


if __name__ == "__main__":
    raise SystemExit(main())
