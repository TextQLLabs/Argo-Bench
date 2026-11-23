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
import pyarrow as pa
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


def _interval_text(value) -> str:
    """A month/day/nanosecond interval the way the engines print one: ``1 day 02:30:00``."""
    parts = []
    years, months = divmod(abs(value.months), 12)
    sign = "-" if value.months < 0 else ""
    parts += [f"{sign}{n} {unit}{'s' if n != 1 else ''}"
              for n, unit in ((years, "year"), (months, "month")) if n]
    if value.days:
        parts.append(f"{value.days} day{'s' if abs(value.days) != 1 else ''}")
    nanos = value.nanoseconds
    if nanos or not parts:
        seconds, fraction = divmod(abs(nanos), 1_000_000_000)
        clock = (f"{'-' if nanos < 0 else ''}{seconds // 3600:02d}:{seconds // 60 % 60:02d}"
                 f":{seconds % 60:02d}")
        if fraction:
            clock += f".{fraction:09d}".rstrip("0")
        parts.append(clock)
    return " ".join(parts)


def _repeats_a_field_name(kind) -> bool:
    """Whether ``kind`` holds a struct two of whose fields share a name. DuckDB's unnamed row
    value ``(a, b)`` names every field "", and neither pandas nor ``to_pylist`` reads that."""
    names = [kind.field(i).name for i in range(kind.num_fields)]
    if pa.types.is_struct(kind) and len(set(names)) < len(names):
        return True
    return any(_repeats_a_field_name(kind.field(i).type) for i in range(kind.num_fields))


def _parquet_holds(kind) -> bool:
    try:
        pq.write_table(pa.table({"c": pa.nulls(0, kind)}), pa.BufferOutputStream())
    except pa.ArrowNotImplementedError:
        return False
    except Exception:                    # not a question of the type: let the real write say
        return True
    return True


def _plain(scalar) -> Any:
    """An Arrow scalar as JSON can write it: a struct whose names repeat becomes a list."""
    kind = scalar.type
    if not scalar.is_valid:
        return None
    if pa.types.is_struct(kind):
        names = [kind.field(i).name for i in range(kind.num_fields)]
        values = [_plain(scalar[i]) for i in range(kind.num_fields)]
        return dict(zip(names, values, strict=True)) if len(set(names)) == len(names) else values
    if kind.num_fields:                                  # a list or a map: its entries
        return [_plain(item) for item in scalar.values]
    value = scalar.as_py()
    return _interval_text(value) if isinstance(value, pa.MonthDayNano) else value


def _text(scalar) -> str | None:
    value = _plain(scalar)
    return value if value is None or isinstance(value, str) else json.dumps(value, default=str)


def storable(table) -> tuple[Any, list[str]]:
    """``table`` as a Parquet file and pandas can both hold it, and a note for the agent on
    each column that had to change.

    Two kinds of column cannot be saved as they come: an INTERVAL (on DuckDB, one timestamp
    minus another), which Parquet has no type for, and a struct whose fields share a name
    (DuckDB's ``(a, b)``), which pandas cannot read back. Either used to fail the whole
    call with no word on why. They are saved, and previewed, as text."""
    notes = []
    for index, field in enumerate(table.schema):
        if _repeats_a_field_name(field.type):
            why = ("its fields share a name. Name them in SQL, as in {'a': x, 'b': y}, to keep "
                   "them apart")
        elif not _parquet_holds(field.type):
            why = ("Parquet has no such type. For a number, take the difference in SQL in the "
                   "unit you need")
        else:
            continue
        text = pa.array([_text(scalar) for chunk in table.column(index).chunks
                         for scalar in chunk], type=pa.large_string())
        table = table.set_column(index, field.name, text)
        kind = str(field.type)
        notes.append(f"note: {field.name} ({kind if len(kind) <= 60 else kind[:59] + '…'}) is "
                     f"saved and shown as text: {why}.")
    return table, notes


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
        table, notes = storable(table)
        try:
            pq.write_table(table, self.results_dir / name)
        except Exception as exc:
            raise WarehouseError(
                f"the query ran, but its result could not be saved ({type(exc).__name__}: "
                f"{str(exc)[:300]}). Cast its columns to plain types (numbers, text, dates, "
                f"timestamps) and run it again.") from exc
        shown = min(self.preview_rows, table.num_rows)
        capped = (f" — capped at {self.max_rows:,} rows; aggregate in SQL or narrow the "
                  f"query for the rest" if result.truncated else "")
        head = (f"{table.num_rows:,} row(s) × {table.num_columns} column(s) in "
                f"{result.elapsed_ms / 1000:.1f}s{capped}")
        if result.stats.get("bytes_processed"):
            head += f" · {result.stats['bytes_processed'] / 1e9:.2f} GB processed"
        more = (f"\n(first {shown} of {table.num_rows:,} rows shown)"
                if table.num_rows > shown else "")
        said = "".join(f"\n{note}" for note in notes)
        return f"{head}\n{render_preview(table, shown)}{more}{said}\nsaved: results/{name}"


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
