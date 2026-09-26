"""The warehouse MCP server: `run_sql`, `list_tables`, `describe_table`.

    python -m argo_bench.servers.warehouse --engine duckdb --database data/argo.duckdb \\
        --schema food_delivery_9 --results-dir <workdir>/results

`run_sql` returns a preview for the agent to read and saves the complete result, up to
``--max-rows``, as Parquet in the run's working directory. That file is how a result reaches
`run_python`, which has no warehouse connection of its own.
"""

from __future__ import annotations

import argparse
import datetime as dt
import decimal
import itertools
import json
import threading
from pathlib import Path
from typing import Any

import anyio
import pyarrow.parquet as pq
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from argo_bench.warehouse import ENGINES, Warehouse, WarehouseError, open_warehouse

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True,
                            openWorldHint=False)


def _cell(value: Any, width: int) -> str:
    if value is None:
        text = "NULL"
    elif isinstance(value, float):
        text = repr(value)
    elif isinstance(value, dt.datetime):
        text = value.isoformat(sep=" ")
    elif isinstance(value, (dt.date, dt.time, decimal.Decimal)):
        text = str(value)
    elif isinstance(value, bytes):
        text = f"<{len(value)} bytes>"
    elif isinstance(value, (dict, list)):
        text = json.dumps(value, default=str)
    else:
        text = str(value)
    text = text.replace("|", "\\|").replace("\r", " ").replace("\n", " ")
    return text if len(text) <= width else text[:width - 1] + "…"


def render_preview(table, rows: int, width: int = 80) -> str:
    """The first ``rows`` rows as a Markdown table, typed in the header."""
    if table.num_columns == 0:
        return "(statement returned no columns)"
    header = [f"{f.name} ({f.type})" for f in table.schema]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for record in table.slice(0, rows).to_pylist():
        lines.append("| " + " | ".join(_cell(record.get(name), width)
                                       for name in table.column_names) + " |")
    return "\n".join(lines)


class WarehouseTools:
    def __init__(self, warehouse: Warehouse, results_dir: Path, preview_rows: int,
                 max_rows: int) -> None:
        self.warehouse = warehouse
        self.results_dir = results_dir
        self.preview_rows = preview_rows
        self.max_rows = max_rows
        self._counter = itertools.count(1)
        self._lock = threading.Lock()

    def list_tables(self) -> str:
        tables = self.warehouse.list_tables()
        return (f"{len(tables)} tables in {self.warehouse.namespace} "
                f"({self.warehouse.dialect}):\n" + "\n".join(tables))

    def describe_table(self, table: str) -> str:
        columns = self.warehouse.describe_table(table)
        lines = [f"{self.warehouse.resolve_table(table)} — {len(columns)} columns"]
        width = max((len(c.name) for c in columns), default=0)
        for column in columns:
            null = "  NOT NULL" if column.nullable is False else ""
            lines.append(f"  {column.name.ljust(width)}  {column.type}{null}")
        return "\n".join(lines)

    def run_sql(self, sql: str) -> str:
        result = self.warehouse.run_sql(sql, max_rows=self.max_rows)
        table = result.table
        with self._lock:
            name = f"sql_{next(self._counter):04d}.parquet"
        self.results_dir.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, self.results_dir / name)
        shown = min(self.preview_rows, table.num_rows)
        capped = (f" — capped at {self.max_rows:,} rows; aggregate in SQL or narrow the "
                  f"query for the rest" if result.truncated else "")
        head = (f"{table.num_rows:,} row(s) × {table.num_columns} column(s) in "
                f"{result.elapsed_ms / 1000:.1f}s{capped}")
        if result.stats.get("bytes_processed"):
            head += f" · {result.stats['bytes_processed'] / 1e9:.2f} GB processed"
        more = (f"\n(first {shown} of {table.num_rows:,} rows shown)"
                if table.num_rows > shown else "")
        return f"{head}\n{render_preview(table, shown)}{more}\nsaved: results/{name}"


def build_server(tools: WarehouseTools) -> MCPServer:
    warehouse = tools.warehouse
    server = MCPServer("warehouse", instructions=(
        f"Read-only access to the {warehouse.namespace} warehouse ({warehouse.dialect})."))

    async def call(fn, *args) -> str:
        try:
            return await anyio.to_thread.run_sync(fn, *args)
        except WarehouseError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(name="run_sql", annotations=READ_ONLY, structured_output=False, description=(
        f"Run one read-only SQL query ({warehouse.dialect}). Unqualified table names "
        f"resolve to {warehouse.namespace}. Returns the first {tools.preview_rows} rows; "
        f"the complete result (up to {tools.max_rows:,} rows) is saved as a Parquet file "
        f"whose path is printed last, for loading in run_python."))
    async def run_sql(sql: str) -> str:
        return await call(tools.run_sql, sql)

    @server.tool(name="list_tables", annotations=READ_ONLY, structured_output=False,
                 description="List every table in the warehouse.")
    async def list_tables() -> str:
        return await call(tools.list_tables)

    @server.tool(name="describe_table", annotations=READ_ONLY, structured_output=False,
                 description="Show a table's columns in order, with types and nullability.")
    async def describe_table(table: str) -> str:
        return await call(tools.describe_table, table)

    return server


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--engine", required=True, choices=ENGINES)
    ap.add_argument("--database", default="", help="DuckDB file, or BigQuery project")
    ap.add_argument("--schema", required=True, help="schema / dataset")
    ap.add_argument("--credentials", default="", help="bigquery: key file, or adc")
    ap.add_argument("--maximum-bytes-billed", type=int, default=None,
                    help="bigquery: per-query scan cap in bytes (default 20 GiB, 0 = none)")
    ap.add_argument("--results-dir", default="results")
    ap.add_argument("--preview-rows", type=int, default=50)
    ap.add_argument("--max-rows", type=int, default=1_000_000)
    ap.add_argument("--timeout", type=float, default=300.0, help="per-query limit, seconds")
    args = ap.parse_args(argv)

    # Connect before serving: a bad credential fails the server's start, not the agent's
    # first query.
    warehouse = open_warehouse(args.engine, database=args.database, schema=args.schema,
                               credentials=args.credentials, timeout_s=args.timeout,
                               maximum_bytes_billed=args.maximum_bytes_billed)
    tools = WarehouseTools(warehouse, Path(args.results_dir).resolve(), args.preview_rows,
                           args.max_rows)
    try:
        build_server(tools).run("stdio")
    finally:
        warehouse.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
