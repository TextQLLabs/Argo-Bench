"""Read-only warehouse access, on DuckDB (a local file) or BigQuery.

Both engines answer the same three calls the same way:

* ``list_tables()``        the table names in the warehouse's one schema;
* ``describe_table(name)`` that table's columns, in order, with their native types;
* ``run_sql(sql)``         one read-only statement, returned as an Arrow table.

Bare table names resolve to the run's schema: ``food_delivery`` for a full-year question,
``food_delivery_9`` (views that stop at the end of September) for one asked as of
September. A statement that names any other schema of the warehouse is refused, so an
as-of question cannot read past its month by qualifying a table name.

Read-only is enforced twice: a lexical guard (one statement, led by a query keyword, no
write keyword outside a literal), then the engine's own — a read-only attach with external
access locked off on DuckDB, a dry run that must report ``SELECT`` on BigQuery.
"""

from __future__ import annotations

import difflib
import re
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa

ENGINES = ("duckdb", "bigquery")
DEFAULT_TIMEOUT_S = 300.0
#: The most one BigQuery query may scan (the final run's setting). 0 = no cap.
DEFAULT_MAXIMUM_BYTES_BILLED = 20 * 1024 ** 3


class WarehouseError(RuntimeError):
    """A lookup or query failed. The message is written to be shown to the agent."""


class ReadOnlyViolation(WarehouseError):
    """The statement is not a single read-only query."""


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    nullable: bool | None = None


@dataclass
class QueryResult:
    table: pa.Table
    truncated: bool
    elapsed_ms: int
    stats: dict = field(default_factory=dict)


# ----------------------------------------------------------------------------- guard
@dataclass(frozen=True)
class Lexicon:
    """How an engine spells strings and comments, which decides where code is."""

    backslash_escapes: bool = False   # 'it\'s' (BigQuery)
    hash_comments: bool = False       # # to end of line (BigQuery)
    dollar_quotes: bool = False       # $$...$$ (DuckDB)


_LEADS = frozenset({"SELECT", "WITH", "SHOW", "DESCRIBE", "DESC", "EXPLAIN", "VALUES",
                    "TABLE", "FROM"})
_WRITES = frozenset({"INSERT", "UPDATE", "DELETE", "MERGE", "UPSERT", "INTO", "CREATE",
                     "DROP", "ALTER", "TRUNCATE", "GRANT", "REVOKE", "CALL", "EXECUTE",
                     "COPY"})
_DOLLAR_TAG = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


def _code_of(sql: str, lex: Lexicon) -> str:
    """The statement with comments removed and every literal blanked to a placeholder."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c, pair = sql[i], sql[i:i + 2]
        if pair == "--" or (lex.hash_comments and c == "#"):
            end = sql.find("\n", i)
            i = n if end < 0 else end
            out.append(" ")
            continue
        if pair == "/*":
            end = sql.find("*/", i + 2)
            if end < 0:
                raise ReadOnlyViolation("unterminated /* comment")
            i = end + 2
            out.append(" ")
            continue
        if c in "'\"`":
            j = i + 1
            while True:
                if j >= n:
                    raise ReadOnlyViolation(f"unterminated {c} quote")
                if lex.backslash_escapes and c != "`" and sql[j] == "\\":
                    j += 2
                    continue
                if sql[j] == c:
                    if j + 1 < n and sql[j + 1] == c:   # doubled quote is an escape
                        j += 2
                        continue
                    break
                j += 1
            out.append(" '' " if c == "'" else " _ident_ ")
            i = j + 1
            continue
        if lex.dollar_quotes and c == "$":
            tag = _DOLLAR_TAG.match(sql, i)
            if tag:
                end = sql.find(tag.group(0), tag.end())
                if end < 0:
                    raise ReadOnlyViolation("unterminated dollar-quoted string")
                out.append(" '' ")
                i = end + len(tag.group(0))
                continue
        out.append(c)
        i += 1
    return "".join(out)


def read_only_statement(sql: str, lex: Lexicon = Lexicon()) -> str:
    """The statement ready to run, or `ReadOnlyViolation` saying why it cannot."""
    if not sql or not sql.strip():
        raise ReadOnlyViolation("empty statement")
    code = _code_of(sql, lex)
    statements = [part for part in code.split(";") if part.strip()]
    if len(statements) != 1:
        raise ReadOnlyViolation(
            f"one statement per call; this has {len(statements)} — run them separately")
    lead = _WORD.search(statements[0].lstrip(" \t\r\n("))
    if lead is None or lead.group(0).upper() not in _LEADS:
        word = lead.group(0).upper() if lead else statements[0].strip()[:20]
        raise ReadOnlyViolation(
            f"only read-only queries run here ({', '.join(sorted(_LEADS))}); "
            f"this statement starts with {word}")
    for word in _WORD.findall(code):
        if word.upper() in _WRITES:
            raise ReadOnlyViolation(
                f"{word.upper()} is not allowed: the warehouse is read-only. If it is a "
                f"column or alias, quote it as an identifier")
    return re.sub(r";\s*$", "", sql.strip())


_QUALIFIED = re.compile(
    r"`?([A-Za-z_][\w$-]*)`?\s*\.\s*`?([A-Za-z_][\w$]*)`?(?:\s*\.\s*`?([A-Za-z_][\w$]*)`?)?")


def qualifiers(sql: str) -> set[str]:
    """Every name used as a schema qualifier in the statement, lower-cased: the first part
    of ``a.b``, the first two of ``a.b.c``. Comments and string literals do not count."""
    text = re.sub(r"--[^\n]*|/\*.*?\*/", " ", sql, flags=re.S)
    text = re.sub(r"'(?:[^'\\]|\\.|'')*'", " '' ", text)
    found = set()
    for first, second, third in _QUALIFIED.findall(text):
        found.add(first.lower())
        if third:
            found.add(second.lower())
    return found


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _unique_names(names: list[str]) -> list[str]:
    """`SELECT a.ID, b.ID` is legal SQL and an illegal Parquet schema; suffix repeats."""
    seen: dict[str, int] = {}
    out = []
    for name in names:
        seen[name] = seen.get(name, 0) + 1
        out.append(name if seen[name] == 1 else f"{name}_{seen[name]}")
    return out


# ------------------------------------------------------------------------------ base
class Warehouse(ABC):
    """One schema of one warehouse, opened read-only."""

    engine = ""
    dialect = ""
    lexicon = Lexicon()

    def __init__(self, *, database: str, schema: str, timeout_s: float = DEFAULT_TIMEOUT_S):
        self.database = database
        self.schema = schema
        self.timeout_s = float(timeout_s)
        self._lock = threading.RLock()
        self._tables: list[str] | None = None
        self._others: set[str] | None = None

    @property
    def namespace(self) -> str:
        return self.schema

    def list_tables(self) -> list[str]:
        with self._lock:
            if self._tables is None:
                self._tables = sorted(self._list_tables(), key=str.upper)
            return list(self._tables)

    def resolve_table(self, name: str) -> str:
        wanted = (name or "").strip().rsplit(".", 1)[-1].strip('`"[] ')
        tables = self.list_tables()
        if wanted in tables:
            return wanted
        folded = [t for t in tables if t.upper() == wanted.upper()]
        if len(folded) == 1:
            return folded[0]
        close = difflib.get_close_matches(wanted.upper(), [t.upper() for t in tables], n=5)
        hint = f"; did you mean {', '.join(close)}?" if close else ""
        raise WarehouseError(f"no table named {name!r} in {self.namespace}{hint}")

    def describe_table(self, name: str) -> list[Column]:
        table = self.resolve_table(name)
        with self._lock:
            return self._describe(table)

    def confine(self, sql: str) -> None:
        """Refuse a statement that names another schema of this warehouse."""
        if self._others is None:
            self._others = {s.lower() for s in self._schemas()} - {self.schema.lower()}
        named = sorted(q for q in qualifiers(sql) if q in self._others)
        if named:
            raise WarehouseError(
                f"this warehouse is {self.schema} and reads nothing else; the statement "
                f"also names {', '.join(named)}. Query the tables in {self.schema}.")

    def run_sql(self, sql: str, max_rows: int = 100_000) -> QueryResult:
        statement = read_only_statement(sql, self.lexicon)
        started = time.monotonic()
        with self._lock:
            self.confine(statement)
            table, truncated, stats = self._run(statement, max(1, int(max_rows)))
        return QueryResult(table=table, truncated=truncated, stats=stats,
                           elapsed_ms=int((time.monotonic() - started) * 1000))

    def close(self) -> None:
        return

    @abstractmethod
    def _schemas(self) -> list[str]: ...

    @abstractmethod
    def _list_tables(self) -> list[str]: ...

    @abstractmethod
    def _describe(self, table: str) -> list[Column]: ...

    @abstractmethod
    def _run(self, sql: str, max_rows: int) -> tuple[pa.Table, bool, dict]: ...


# ---------------------------------------------------------------------------- DuckDB
class DuckDBWarehouse(Warehouse):
    """A DuckDB file attached read-only with the filesystem fenced off: the agent's SQL
    can read the warehouse and nothing else on the host."""

    engine = "duckdb"
    dialect = "DuckDB SQL"
    lexicon = Lexicon(dollar_quotes=True)

    def __init__(self, *, database: str, schema: str, timeout_s: float = DEFAULT_TIMEOUT_S,
                 **_: Any) -> None:
        super().__init__(database=database, schema=schema, timeout_s=timeout_s)
        import duckdb

        path = Path(database).expanduser().resolve()
        if not path.is_file():
            raise WarehouseError(f"no DuckDB warehouse at {path} (scripts/load_duckdb.py)")
        self.conn = duckdb.connect()
        self.conn.execute(f"ATTACH {_sql_literal(str(path))} AS warehouse (READ_ONLY)")
        self.conn.execute(f'USE warehouse."{schema}"')
        self.conn.execute(f"SET allowed_paths = [{_sql_literal(str(path))}]")
        self.conn.execute("SET enable_external_access = false")
        self.conn.execute("SET lock_configuration = true")

    def _query(self, sql: str, max_rows: int) -> tuple[pa.Table, bool]:
        import duckdb

        timer = threading.Timer(self.timeout_s, self.conn.interrupt)
        timer.start()
        try:
            result = self.conn.execute(sql)
            batches_of = getattr(result, "to_arrow_reader", None) or result.fetch_record_batch
            reader = batches_of(min(max_rows + 1, 100_000))
            batches, rows = [], 0
            for batch in reader:
                batches.append(batch)
                rows += batch.num_rows
                if rows > max_rows:
                    break
            table = pa.Table.from_batches(batches) if batches else reader.schema.empty_table()
            table = table.rename_columns(_unique_names(table.column_names))
            return table.slice(0, max_rows), rows > max_rows
        except duckdb.InterruptException as exc:
            raise WarehouseError(f"query exceeded {self.timeout_s:.0f}s and was "
                                 f"interrupted") from exc
        except duckdb.Error as exc:
            raise WarehouseError(str(exc)) from exc
        finally:
            timer.cancel()

    def _schemas(self) -> list[str]:
        table, _ = self._query("SELECT schema_name FROM information_schema.schemata "
                               "WHERE catalog_name = current_database() AND schema_name "
                               "NOT IN ('information_schema', 'pg_catalog')", 100_000)
        return [str(v) for v in table.column(0).to_pylist()]

    def _list_tables(self) -> list[str]:
        table, _ = self._query("SELECT table_name FROM information_schema.tables "
                               "WHERE table_catalog = current_database() "
                               "AND table_schema = current_schema()", 1_000_000)
        return [str(v) for v in table.column(0).to_pylist()]

    def _describe(self, table: str) -> list[Column]:
        result, _ = self._query(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_catalog = current_database() AND table_schema = current_schema() "
            f"AND table_name = {_sql_literal(table)} ORDER BY ordinal_position", 100_000)
        return [Column(name, kind, None if null is None else str(null).upper() == "YES")
                for name, kind, null in zip(*(result.column(i).to_pylist() for i in range(3)))]

    def _run(self, sql: str, max_rows: int) -> tuple[pa.Table, bool, dict]:
        table, truncated = self._query(sql, max_rows)
        return table, truncated, {}

    def close(self) -> None:
        self.conn.close()


# -------------------------------------------------------------------------- BigQuery
def google_credentials(spec: str | None):
    """A service-account key file, or ``adc`` / empty for Application Default Credentials."""
    import google.auth

    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    if not spec or spec == "adc":
        return google.auth.default(scopes=scopes)
    from google.oauth2 import service_account

    path = Path(spec).expanduser().resolve()
    if not path.is_file():
        raise WarehouseError(f"BigQuery credentials file not found: {path}")
    creds = service_account.Credentials.from_service_account_file(str(path), scopes=scopes)
    return creds, creds.project_id


class BigQueryWarehouse(Warehouse):
    """A BigQuery dataset. ``database`` is the project the release was loaded into."""

    engine = "bigquery"
    dialect = "BigQuery GoogleSQL"
    lexicon = Lexicon(backslash_escapes=True, hash_comments=True)

    def __init__(self, *, database: str, schema: str, credentials: str | None = None,
                 maximum_bytes_billed: int | None = None, timeout_s: float = DEFAULT_TIMEOUT_S,
                 **_: Any) -> None:
        super().__init__(database=database, schema=schema, timeout_s=timeout_s)
        from google.cloud import bigquery

        creds, default_project = google_credentials(credentials)
        self._bq = bigquery
        self.maximum_bytes_billed = (DEFAULT_MAXIMUM_BYTES_BILLED if maximum_bytes_billed is None
                                     else int(maximum_bytes_billed) or None)
        self.client = bigquery.Client(project=database or default_project, credentials=creds)
        self.dataset = f"{self.client.project}.{schema}"

    def _error(self, exc: Exception) -> WarehouseError:
        errors = getattr(exc, "errors", None) or []
        first = errors[0] if errors and isinstance(errors[0], dict) else {}
        text = str(first.get("message") or getattr(exc, "message", None) or exc)
        # The agent is shown a dataset, never the project that holds it.
        return WarehouseError(text.replace(f"{self.client.project}:", "")
                              .replace(f"{self.client.project}.", "")
                              .replace(self.client.project, "the project"))

    def _schemas(self) -> list[str]:
        return [d.dataset_id for d in self.client.list_datasets(self.client.project)]

    def _list_tables(self) -> list[str]:
        from google.api_core import exceptions as gexc
        try:
            return [t.table_id for t in self.client.list_tables(self.dataset)]
        except gexc.GoogleAPICallError as exc:
            raise self._error(exc) from exc

    def _describe(self, table: str) -> list[Column]:
        from google.api_core import exceptions as gexc
        try:
            schema = self.client.get_table(f"{self.dataset}.{table}").schema
        except gexc.GoogleAPICallError as exc:
            raise self._error(exc) from exc
        return [Column(f.name, f"ARRAY<{f.field_type}>" if f.mode == "REPEATED"
                       else f.field_type, f.mode != "REQUIRED") for f in schema]

    def _run(self, sql: str, max_rows: int) -> tuple[pa.Table, bool, dict]:
        from google.api_core import exceptions as gexc

        bq = self._bq
        try:
            dry = self.client.query(sql, job_config=bq.QueryJobConfig(
                default_dataset=self.dataset, dry_run=True, use_query_cache=False))
            if dry.statement_type != "SELECT":
                raise ReadOnlyViolation(f"BigQuery classifies this statement as "
                                        f"{dry.statement_type}; only SELECT runs here")
            cap, estimate = self.maximum_bytes_billed, dry.total_bytes_processed or 0
            if cap and estimate > cap:
                raise WarehouseError(
                    f"this query would scan {estimate / 1024 ** 3:,.1f} GB; a query here may "
                    f"scan at most {cap / 1024 ** 3:,.1f} GB. Narrow it: select only the "
                    f"columns you need (BigQuery reads every column you name, in full), "
                    f"filter on dates, and aggregate in a subquery before joining large "
                    f"tables.")
            settings: dict = {"default_dataset": self.dataset}
            if cap:
                settings["maximum_bytes_billed"] = int(cap)
            job = self.client.query(sql, job_config=bq.QueryJobConfig(**settings))
            try:
                rows = job.result(timeout=self.timeout_s, max_results=max_rows)
            except TimeoutError as exc:
                job.cancel()
                raise WarehouseError(
                    f"query exceeded {self.timeout_s:.0f}s and was cancelled") from exc
            table = rows.to_arrow(create_bqstorage_client=False)
        except gexc.GoogleAPICallError as exc:
            raise self._error(exc) from exc
        return (table, (rows.total_rows or 0) > max_rows,
                {"bytes_processed": job.total_bytes_processed})


_CLASSES = {"duckdb": DuckDBWarehouse, "bigquery": BigQueryWarehouse}


def open_warehouse(engine: str, **settings: Any) -> Warehouse:
    if engine not in _CLASSES:
        raise WarehouseError(f"unknown engine {engine!r}; supported: {', '.join(ENGINES)}")
    return _CLASSES[engine](**{k: v for k, v in settings.items() if v not in (None, "")})
