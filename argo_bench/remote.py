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
"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import time

import httpx
from inspect_ai.scorer import Score, Target, mean, scorer
from inspect_ai.solver import TaskState

from argo_bench.agent import FILINGS

GRADER_URL = "https://grade.argo-bench.com"
KEY_ENV = "ARGO_GRADER_KEY"
URL_ENV = "ARGO_GRADER_URL"
RETRY_FOR_S = 20 * 60
RETRYABLE = {429, 500, 502, 503, 504}


class GraderRefused(RuntimeError):
    """The grading service refused the request for a reason retrying will not fix."""


def grader_key() -> str:
    return os.environ.get(KEY_ENV, "").strip()


def grader_url() -> str:
    return (os.environ.get(URL_ENV, "").strip() or GRADER_URL).rstrip("/")


async def grade(question_id: str, version: str, filings: list[dict], *, model: str = "",
                run: str = "", client: httpx.AsyncClient | None = None) -> dict:
    """Grade one run's filings: ``{"grade_id", "question_id", "reward"}``."""
    key = grader_key()
    if not key:
        raise GraderRefused(f"{KEY_ENV} is not set: an API key from the Argo-Bench team is "
                            "needed to score runs")
    body = gzip.compress(json.dumps({
        "question_id": question_id, "question_version": version, "filings": filings,
        "harness": "inspect", "model": model, "run": run,
    }, default=str).encode("utf-8"))
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
