"""The reference solutions, replayed through the tools a model gets.

``-T solver=reference`` runs a question's reference solution (``reference/<question>.py``)
instead of a model. A reference is a list of tool calls, ``STEPS``; each is made in turn
through the run's own two MCP servers, exactly as a model's turn would make it, and what it
files is kept like any other run's. No model is called, so ``--model mockllm/model`` will do.

A step is ``(tool, arguments)`` with ``tool`` one of ``run_sql``, ``list_tables``,
``describe_table`` or ``run_python``. A ``run_sql`` statement that the two engines spell
differently is given as ``{"bigquery": ..., "duckdb": ...}`` and the run's engine picks one.
A reference whose calls depend on the prompt (a question that names entities drawn from
the warehouse) defines ``steps(prompt)`` instead of ``STEPS``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from inspect_ai.model import ChatMessageAssistant, execute_tools
from inspect_ai.solver import Generate, TaskState, solver
from inspect_ai.tool import ToolCall, mcp_connection

from argo_bench.agent import close_run, open_run

REFERENCES = Path(__file__).resolve().parents[1] / "reference"
TOOLS = ("run_sql", "list_tables", "describe_table", "run_python")


def module_path(question: str) -> Path:
    return REFERENCES / f"{question.replace('-', '_')}.py"


def available() -> list[str]:
    """The questions that have a reference solution."""
    return sorted(load(p.stem.replace("_", "-"), p).QUESTION
                  for p in REFERENCES.glob("*.py") if not p.name.startswith("_"))


def load(question: str, path: Path | None = None) -> ModuleType:
    path = path or module_path(question)
    if not path.is_file():
        raise ValueError(f"no reference solution for {question} ({path.name})")
    spec = importlib.util.spec_from_file_location(f"reference_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def steps(module: ModuleType, prompt: str, engine: str) -> list[tuple[str, dict]]:
    """The reference's tool calls for this prompt, spelled for ``engine``."""
    raw = module.steps(prompt) if hasattr(module, "steps") else module.STEPS
    out = []
    for tool, arguments in raw:
        if tool not in TOOLS:
            raise ValueError(f"{module.QUESTION}: unknown tool {tool!r}")
        arguments = dict(arguments)
        if isinstance(arguments.get("sql"), dict):
            arguments["sql"] = arguments["sql"][engine]
        out.append((tool, arguments))
    return out


@solver
def reference_solver(engine: str, database: str, schema: str, credentials: str = "",
                     sandbox: str = "local", image: str = "argo-sandbox", python: str = "",
                     workdir_root: str = "", keep_workdirs: bool = False):
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        module = load(str(state.sample_id))
        calls = steps(module, state.input_text, engine)
        workdir, tools = open_run(state, engine, database, schema, credentials, sandbox,
                                  image, python, workdir_root)
        try:
            async with mcp_connection(tools):
                for n, (tool, arguments) in enumerate(calls, 1):
                    call = ToolCall(id=f"reference-{n}", function=tool, arguments=arguments)
                    state.messages.append(ChatMessageAssistant(content="", tool_calls=[call]))
                    result = await execute_tools(state.messages, tools)
                    state.messages.extend(result.messages)
                    failed = next((m for m in result.messages
                                   if getattr(m, "error", None) is not None), None)
                    if failed is not None:
                        raise RuntimeError(f"{module.QUESTION}: step {n} ({tool}) failed: "
                                           f"{failed.error.message}")
        finally:
            close_run(state, workdir, keep_workdirs)
        return state

    return solve
