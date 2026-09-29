"""The reference agent: a plain tool loop on Inspect AI.

The model gets the system prompt, the question, and the tools of two MCP servers, nothing
else:

* the warehouse: ``run_sql``, ``list_tables``, ``describe_table`` (`servers/warehouse.py`);
* a Python interpreter: ``run_python`` (`servers/python.py`), in which it imports the
  Mission Control console and files its findings.

It calls tools until it answers without calling one, or runs out of turns or time. There is
no planner, no memory, no retrieval and no retry logic.

Each sample gets its own working directory (the console, the question's dashboard
contracts if it has any, ``run_sql``'s Parquet results) and its own pair of servers. What
the run filed — the console's journal — is kept in the sample's store under ``filings``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

from inspect_ai.model import ChatMessageSystem, execute_tools, get_model
from inspect_ai.solver import Generate, TaskState, solver
from inspect_ai.tool import mcp_connection, mcp_server_stdio, mcp_tools

from argo_bench.prompt import dataset_for, system_prompt

PACKAGE = Path(__file__).resolve().parent
CONSOLE = (PACKAGE / "console" / "mission_control.py",
           PACKAGE / "console" / "mission_control_plumbing.py")
CONTRACTS_FILE = "mission_control_contracts.json"
JOURNAL = "mission_control_actions.jsonl"
FILINGS = "filings"

#: Environment each server may need from the launching process, by prefix. A server gets
#: only its own: the warehouse credentials never reach the Python server (or the kernel).
WAREHOUSE_ENV = ("GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT", "CLOUDSDK_",
                 "SSL_CERT_")
PYTHON_ENV = ("KUBECONFIG", "DOCKER_", "SSL_CERT_")


def forwarded(prefixes: tuple[str, ...]) -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k.startswith(prefixes)}


def prepare_workdir(root: str, name: str, contracts: dict | None) -> Path:
    if root:
        Path(root).mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix=f"{name}-", dir=root or None))
    for path in CONSOLE:
        shutil.copy(path, workdir / path.name)
    if contracts:
        (workdir / CONTRACTS_FILE).write_text(json.dumps(contracts, indent=2) + "\n",
                                              encoding="utf-8")
    return workdir


def read_journal(workdir: Path) -> list[dict]:
    path = workdir / JOURNAL
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def question_view(card: dict, engine: str, schema: str) -> tuple[str, str]:
    """The dataset a question reads and the system prompt it is shown, from its cutoff."""
    cutoff = card.get("data_cutoff", "")
    return dataset_for(schema, cutoff), system_prompt(engine, cutoff)


def open_run(state: TaskState, engine: str, database: str, schema: str, credentials: str,
             sandbox: str, image: str, python: str, workdir_root: str) -> tuple[Path, list]:
    """One run's working directory and the tools of its own two servers. The system prompt
    goes first in the conversation."""
    card = state.metadata
    dataset, system = question_view(card, engine, schema)
    name = re.sub(r"[^a-z0-9-]+", "-", f"argo-{state.sample_id}".lower())[:40].strip("-")
    run_id = f"{name}-{uuid.uuid4().hex[:6]}"
    workdir = prepare_workdir(workdir_root, name, card.get("contracts"))
    path = {"PYTHONPATH": str(PACKAGE.parent)}
    warehouse = mcp_server_stdio(
        name="warehouse", command=sys.executable, cwd=workdir,
        args=["-m", "argo_bench.servers.warehouse", "--engine", engine,
              "--database", database, "--schema", dataset,
              "--credentials", credentials, "--results-dir", str(workdir / "results")],
        env={**forwarded(WAREHOUSE_ENV), **path})
    interpreter = mcp_server_stdio(
        name="python", command=sys.executable, cwd=workdir,
        args=["-m", "argo_bench.servers.python", "--cwd", str(workdir),
              "--backend", sandbox, "--name", run_id, "--image", image,
              *(["--python", python] if python else []),
              "--env", "MISSION_CONTROL_TARGET=", "--env",
              f"MISSION_CONTROL_SANDBOX_ID={run_id}"],
        env={**forwarded(PYTHON_ENV), **path})
    state.messages.insert(0, ChatMessageSystem(content=system))
    return workdir, [mcp_tools(warehouse), mcp_tools(interpreter)]


def close_run(state: TaskState, workdir: Path, keep_workdirs: bool) -> None:
    state.store.set(FILINGS, read_journal(workdir))
    if not keep_workdirs:
        shutil.rmtree(workdir, ignore_errors=True)


@solver
def argo_agent(engine: str, database: str, schema: str, credentials: str = "",
               sandbox: str = "local", image: str = "argo-sandbox", python: str = "",
               max_turns: int = 500, workdir_root: str = "", keep_workdirs: bool = False):
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        workdir, tools = open_run(state, engine, database, schema, credentials, sandbox,
                                  image, python, workdir_root)
        try:
            async with mcp_connection(tools):
                for _ in range(max_turns):
                    state.output = await get_model().generate(state.messages, tools=tools)
                    state.messages.append(state.output.message)
                    if not state.output.message.tool_calls:
                        break
                    result = await execute_tools(state.messages, tools)
                    state.messages.extend(result.messages)
        finally:
            close_run(state, workdir, keep_workdirs)
        return state

    return solve
