"""Argo-Bench on Inspect AI.

    inspect eval argo_bench/task.py --model anthropic/claude-opus-5-5 --reasoning-effort high
    inspect eval argo_bench/task.py -T questions=smoke --model openai/gpt-6-luna
    inspect eval argo_bench/task.py -T sandbox=docker -T questions=fraud-04-stolen-orders-brooklyn ...

Task options (``-T name=value``); the warehouse ones default to ``ARGO_WAREHOUSE_*``:

``questions``   ``final`` (the 210 questions, tasks/final.jsonl), ``smoke`` (a no-data check of
                the whole path), a path to a JSONL of cards, or comma-separated question ids
``engine``      ``duckdb`` (default) or ``bigquery``
``database``    the DuckDB file (default ``data/argo.duckdb``) or the BigQuery project
``schema``      the base schema / dataset (default ``food_delivery``)
``credentials`` BigQuery: a service-account key file, or ``adc``
``sandbox``     where ``run_python`` runs: ``local`` (default), ``docker`` or ``k8s``
``image``       docker / k8s: the sandbox image (default ``argo-sandbox``)
``python``      local: the kernel's interpreter (default: this one)

The limits are the benchmark's: 240 minutes of wall clock and 500 model turns per question.
Nothing here is scored against an answer key; see README.md, *Submitting results*.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from pathlib import Path

from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.scorer import Score, Target, mean, scorer
from inspect_ai.solver import TaskState

from argo_bench.agent import FILINGS, argo_agent

REPO = Path(__file__).resolve().parents[1]
CARDS = REPO / "tasks"
TIME_LIMIT_S = 14400
MAX_TURNS = 500


def load_cards(questions: str) -> list[dict]:
    def read(path: Path) -> list[dict]:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    if questions in ("final", "smoke"):
        return read(CARDS / f"{questions}.jsonl")
    if questions.endswith(".jsonl"):
        return read(Path(questions))
    wanted = [q.strip() for q in questions.split(",") if q.strip()]
    cards = {c["id"]: c for c in read(CARDS / "final.jsonl") + read(CARDS / "smoke.jsonl")}
    unknown = [q for q in wanted if q not in cards]
    if unknown:
        raise ValueError(f"unknown question id(s): {', '.join(unknown)}")
    return [cards[q] for q in wanted]


def empty_duckdb(schema: str) -> str:
    """A throwaway warehouse for the smoke question, which queries nothing."""
    import duckdb

    path = Path(tempfile.mkdtemp(prefix="argo-smoke-")) / "empty.duckdb"
    with duckdb.connect(str(path)) as con:
        con.execute(f'CREATE SCHEMA "{schema}"')
    return str(path)


@scorer(metrics=[mean()])
def filed():
    """1 if the run filed anything through Mission Control, else 0. Not a grade: the answer
    keys are held out. The filings themselves are in the sample's store."""

    async def score(state: TaskState, target: Target) -> Score:
        filings = state.store.get(FILINGS, [])
        kinds = Counter(f.get("kind", "?") for f in filings)
        decisions = sum(len((f.get("payload") or {}).get("items") or [f]) for f in filings)
        return Score(value=1 if filings else 0,
                     answer=", ".join(f"{k}={n}" for k, n in sorted(kinds.items())),
                     explanation=f"{decisions} decision(s) in {len(filings)} filing(s)",
                     metadata={"actions": len(filings), "decisions": decisions})

    return score


def matches(item: dict, want: dict) -> bool:
    for key, value in want.items():
        got = item.get(key)
        if isinstance(value, float):
            try:
                if abs(float(got) - value) >= 0.01:
                    return False
            except (TypeError, ValueError):
                return False
        elif str(got) != str(value):
            return False
    return True


@scorer(metrics=[mean()])
def conformance():
    """The smoke question's key is its own prompt: every value it names must be filed."""

    async def score(state: TaskState, target: Target) -> Score:
        filed_items = [(f.get("kind"), item) for f in state.store.get(FILINGS, [])
                       for item in (f.get("payload") or {}).get("items") or []]
        missing = [f"{kind} {want}" for kind, wants in
                   (state.metadata.get("expected") or {}).items() for want in wants
                   if not any(k == kind and matches(item, want) for k, item in filed_items)]
        return Score(value=0 if missing else 1,
                     explanation="missing: " + "; ".join(missing) if missing else "all filed")

    return score


@task
def argo_bench(questions: str = "final",
               engine: str = os.environ.get("ARGO_WAREHOUSE_ENGINE", "duckdb"),
               database: str = os.environ.get("ARGO_WAREHOUSE_DATABASE", "data/argo.duckdb"),
               schema: str = os.environ.get("ARGO_WAREHOUSE_SCHEMA", "food_delivery"),
               credentials: str = os.environ.get("ARGO_WAREHOUSE_CREDENTIALS", ""),
               sandbox: str = os.environ.get("ARGO_SANDBOX", "local"),
               image: str = os.environ.get("ARGO_SANDBOX_IMAGE", "argo-sandbox"),
               python: str = os.environ.get("ARGO_SANDBOX_PYTHON", ""),
               workdir_root: str = "", keep_workdirs: bool = False) -> Task:
    cards = load_cards(str(questions))
    smoke_only = all(c["id"].startswith("smoke-") for c in cards)
    if engine == "duckdb":
        if smoke_only and not Path(database).is_file():
            database = empty_duckdb(schema)
        database = str(Path(database).expanduser().resolve())
    if credentials and credentials != "adc":
        credentials = str(Path(credentials).expanduser().resolve())
    dataset = [Sample(id=c["id"], input=c["prompt"],
                      metadata={k: v for k, v in c.items() if k not in ("id", "prompt")})
               for c in cards]
    return Task(
        dataset=dataset,
        solver=argo_agent(engine=engine, database=database, schema=schema,
                          credentials=credentials, sandbox=sandbox, image=image, python=python,
                          max_turns=MAX_TURNS, workdir_root=workdir_root,
                          keep_workdirs=keep_workdirs),
        scorer=[filed(), conformance()] if smoke_only else filed(),
        time_limit=TIME_LIMIT_S,
        name="argo-bench",
    )
