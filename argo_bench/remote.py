"""Scoring with the Argo-Bench grading service.

The answer keys are held by the grading service, not shipped here. With an API key in
``ARGO_GRADER_KEY``, a run's filings (the sample store's ``filings``) are sent to the service,
and the reward it returns is the question's score:

    export ARGO_GRADER_KEY=argo_...
    inspect eval argo_bench/task.py --model ...                     # scored as each question ends
    inspect score logs/<log>.eval --scorer argo_bench/remote.py@remote_grade \
        --action append --overwrite                                 # a finished log
    .venv/bin/python scripts/grade_remote.py --log-dir logs         # a table over many logs

Only the reward comes back: the per-metric detail would name answer-key entities. Busy or
rate-limited answers (HTTP 429, 503) are retried for up to 20 minutes; any other refusal (a
missing, revoked or invalid key, a question version the service does not grade) raises with
the service's message, so the sample shows a scoring error instead of a score of 0.

The service grades every release of the benchmark. Each card in ``tasks/final.jsonl`` names
its ``grading_key``: the answer key and grading code of this release's version of the question,
so a run is graded against the warehouse its card was written for. A run whose question id
and version match no card here (a log from an earlier release) is sent without one.

Filings the grader skips are left out before sending: a filing already sent with the same
kind and payload (the grader keeps the first copy), and records that are not filings. The
grade is the grade of every filing; only the request is smaller.
"""

from __future__ import annotations

import asyncio
import functools
import gzip
import hashlib
import json
import os
import time
from pathlib import Path

import httpx
from inspect_ai.scorer import Score, Target, mean, scorer
from inspect_ai.solver import TaskState

from argo_bench.agent import FILINGS

GRADER_URL = "https://grade.argo-bench.com"
KEY_ENV = "ARGO_GRADER_KEY"
URL_ENV = "ARGO_GRADER_URL"
RETRY_FOR_S = 20 * 60
RETRYABLE = {429, 500, 502, 503, 504}
#: The service's limits (413 beyond either): the body as sent, and once decompressed.
MAX_SENT = 512 * 1024 * 1024
MAX_DECOMPRESSED = 2 * 1024 * 1024 * 1024
CARDS = Path(__file__).resolve().parents[1] / "tasks" / "final.jsonl"


class GraderRefused(RuntimeError):
    """The grading service refused the request for a reason retrying will not fix."""


def grader_key() -> str:
    return os.environ.get(KEY_ENV, "").strip()


def grader_url() -> str:
    return (os.environ.get(URL_ENV, "").strip() or GRADER_URL).rstrip("/")


@functools.cache
def grading_keys() -> dict[tuple[str, str], str]:
    """(question id, version) -> the grading key its card names."""
    if not CARDS.is_file():
        return {}
    out = {}
    for line in CARDS.read_text(encoding="utf-8").splitlines():
        if line.strip():
            card = json.loads(line)
            if card.get("grading_key"):
                out[(card["id"], card.get("version", ""))] = card["grading_key"]
    return out


def grading_key(question_id: str, version: str) -> str:
    """The grading key of this release's card for the question at ``version`` ("" if none)."""
    return grading_keys().get((question_id, version), "")


def compact_filings(filings: list) -> list:
    """The filings less those the grader skips, in order: a filing with the same kind and
    payload as one before it, a session handshake, a record with no kind. The service writes
    each record as a JSON line and reads it back, so they are compared as it reads them."""
    seen: set[bytes] = set()
    out = []
    for record in filings:
        try:
            read = json.loads(json.dumps(record, default=str))
        except (TypeError, ValueError):
            out.append(record)              # the service decides what to make of it
            continue
        if not isinstance(read, dict):
            out.append(record)              # refused by the service (400), not skipped
            continue
        if read.get("kind") in (None, "handshake"):
            continue
        payload = read.get("payload") if isinstance(read.get("payload"), dict) else {}
        identity = [str(read["kind"]), json.dumps(payload, sort_keys=True, default=str)]
        key = hashlib.sha256(json.dumps(identity).encode("utf-8")).digest()
        if key in seen:
            continue
        seen.add(key)
        out.append(record)
    return out


async def grade(question_id: str, version: str, filings: list[dict], *, model: str = "",
                run: str = "", client: httpx.AsyncClient | None = None) -> dict:
    """Grade one run's filings: ``{"grade_id", "question_id", "reward"}``."""
    key = grader_key()
    if not key:
        raise GraderRefused(f"{KEY_ENV} is not set: an API key from the Argo-Bench team is "
                            "needed to score runs")
    routed = grading_key(question_id, version)
    raw = json.dumps({
        "question_id": question_id, "question_version": version,
        **({"grading_key": routed} if routed else {}),
        "filings": compact_filings(filings), "harness": "inspect", "model": model, "run": run,
    }, default=str).encode("utf-8")
    if len(raw) > MAX_DECOMPRESSED:
        raise GraderRefused(f"{question_id}: the filings are {len(raw):,} bytes uncompressed, "
                            f"over the grading service's {MAX_DECOMPRESSED:,}; not sent")
    body = gzip.compress(raw)
    del raw
    if len(body) > MAX_SENT:
        raise GraderRefused(f"{question_id}: the request is {len(body):,} bytes compressed, "
                            f"over the grading service's {MAX_SENT:,}; not sent")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "Content-Encoding": "gzip", "User-Agent": "argo-bench-inspect/1"}
    own = client is None
    client = client or httpx.AsyncClient(timeout=960)
    deadline, wait = time.monotonic() + RETRY_FOR_S, 5.0
    try:
        while True:
            try:
                response = await client.post(f"{grader_url()}/v1/grade", content=body,
                                             headers=headers)
            except httpx.TransportError as exc:
                if time.monotonic() > deadline:
                    raise GraderRefused(f"grading service unreachable at {grader_url()}: {exc}")
                retry = wait
            else:
                if response.status_code == 200:
                    return response.json()
                try:
                    message = response.json().get("error", "")
                except ValueError:
                    message = response.text[:200]
                if response.status_code not in RETRYABLE or time.monotonic() > deadline:
                    raise GraderRefused(f"{question_id}: grading service refused "
                                        f"({response.status_code}): {message}")
                retry = float(response.headers.get("retry-after") or wait)
            await asyncio.sleep(min(retry, 120))
            wait = min(wait * 2, 120)
    finally:
        if own:
            await client.aclose()


@scorer(metrics=[mean()])
def remote_grade():
    """The question's score, from the grading service (needs ``ARGO_GRADER_KEY``)."""

    async def score(state: TaskState, target: Target) -> Score:
        version = (state.metadata or {}).get("version", "")
        result = await grade(str(state.sample_id), version, state.store.get(FILINGS, []),
                             model=str(state.model), run=f"epoch {state.epoch}")
        return Score(value=result["reward"], explanation=f"grade {result['grade_id']}",
                     metadata={"grade_id": result["grade_id"], "grader": grader_url()})

    return score
