"""Read-only warehouse access, on DuckDB (a local file) or BigQuery.

Both engines answer the same three calls the same way:

* ``list_tables()``        the table names in the warehouse's one schema;
* ``describe_table(name)`` that table's columns, in order, with their native types;
* ``run_sql(sql)``         one read-only statement, returned as an Arrow table.

Bare table names resolve to the run's schema: ``food_delivery`` for a full-year question,
``food_delivery_9`` (views that stop at the end of September) for one asked as of
September. A statement that reads any other schema of the warehouse is refused, so an
as-of question cannot read past its month. On DuckDB the check reads DuckDB's own parse of
the statement (every table it names, in any spelling or quoting), refuses table functions
that run SQL from a string or read files, answers catalog queries for the run's schema
only, and drops error hints that name another schema. On BigQuery it reads the dataset
names the statement spells out, with quoted names' escapes resolved.

Read-only is enforced twice: a lexical guard (one statement, led by a query keyword, no
write keyword outside a literal), then the engine's own — a read-only attach with external
access locked off on DuckDB, a dry run that must report ``SELECT`` on BigQuery.
"""

from __future__ import annotations

import difflib
import json
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


_ESCAPE = re.compile(
    r"\\(?:[xX]([0-9A-Fa-f]{2})|u([0-9A-Fa-f]{4})|U([0-9A-Fa-f]{8})|([0-7]{3})|(.))", re.S)
_SIMPLE_ESCAPES = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t",
                   "v": "\v"}


def _unescape(text: str) -> str:
    """A quoted identifier with its escapes resolved, as BigQuery resolves them:
    ``food_deliver\\x79`` names ``food_delivery``."""
    def one(m: re.Match) -> str:
        hex2, hex4, hex8, octal, other = m.groups()
        try:
            if hex2 or hex4 or hex8:
                return chr(int(hex2 or hex4 or hex8, 16))
            if octal:
                return chr(int(octal, 8))
        except (ValueError, OverflowError):
            return ""
        return _SIMPLE_ESCAPES.get(other, other)
    return _ESCAPE.sub(one, text)


def _names_text(sql: str) -> str:
    """The statement as BigQuery reads names in it: comments (``--``, ``#``, ``/* */``) and
    string literals (``'..'``, ``".."``, triple-quoted) blanked, backticked identifiers kept
    with their escapes resolved. One pass, left to right, so a ``--`` inside a string, or a
    quote inside a double-quoted string, is read the way BigQuery reads it."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c, pair = sql[i], sql[i:i + 2]
        if pair == "--" or c == "#":
            end = sql.find("\n", i)
            i = n if end < 0 else end
            out.append(" ")
            continue
        if pair == "/*":
            end = sql.find("*/", i + 2)
            i = n if end < 0 else end + 2
            out.append(" ")
            continue
        if c in "'\"`":
            quote = c * 3 if c != "`" and sql.startswith(c * 3, i) else c
            j = i + len(quote)
            while j < n and not sql.startswith(quote, j):
                j += 2 if sql[j] == "\\" else 1
            body = sql[i + len(quote):min(j, n)]
            i = min(n, j + len(quote))
            out.append(f"`{_unescape(body)}`" if c == "`" else " '' ")
            continue
        out.append(c)
        i += 1
    return "".join(out)


def qualifiers(sql: str) -> set[str]:
    """Every name used as a schema qualifier in the statement, lower-cased: the first part
    of ``a.b``, the first two of ``a.b.c``. Comments and string literals do not count;
    backticked names count with their escapes resolved (BigQuery's quoting)."""
    text = _names_text(sql)
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

    def other_schemas(self) -> set[str]:
        """Every schema of the warehouse except this one, lower-cased. Read once."""
        if self._others is None:
            self._others = {s.lower() for s in self._schemas()} - {self.schema.lower()}
        return self._others

    def refuse_others(self, named) -> None:
        named = sorted({str(n) for n in named})
        if named:
            raise WarehouseError(
                f"this warehouse is {self.schema} and reads nothing else; the statement "
                f"also names {', '.join(named)}. Query the tables in {self.schema}.")

    def confine(self, sql: str) -> str:
        """The statement to run, or `WarehouseError` when it names another schema of this
        warehouse. This version reads the names spelled out in the text (BigQuery)."""
        self.refuse_others(q for q in qualifiers(sql) if q in self.other_schemas())
        return sql

    def run_sql(self, sql: str, max_rows: int = 100_000) -> QueryResult:
        statement = read_only_statement(sql, self.lexicon)
        started = time.monotonic()
        with self._lock:
            statement = self.confine(statement)
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


# ---------------------------------------------------------------- DuckDB confinement
class DuckDBConfinement:
    """Keeps a DuckDB connection's statements inside one schema of an attached warehouse.

    The warehouse is attached as catalog ``warehouse`` and the run's schema is the current
    one, so a bare name reads it; but every other schema of the file stays one qualified
    name away, and DuckDB has no per-schema grants. So each statement is read through
    DuckDB's own parse of it (``json_serialize_sql``), which names the schema of every
    table it reads however the name is spelled or quoted:

    * a table must be one of this schema's, or a CTE of the statement's own;
    * table functions that run SQL from a string, read files or sample a table
      (``query``, ``query_table``, ``read_*``, ``histogram``, ...) are refused;
    * catalog queries (information_schema, duckdb_tables(), SHOW ALL TABLES, ...) are
      rewritten to describe this schema only, with definitions blanked: a month's view
      definition names the base schema and its cut;
    * DuckDB's "Did you mean" hints that name another schema are dropped from errors.

    Views of this schema still read the base tables they are defined on: only the
    statement's own references are checked. The same class sits in the internal harness
    (evaluation/argo/warehouse.py) and the public export (argo_bench/warehouse.py); a test
    keeps the two copies identical. Hosts provide ``schema``, ``list_tables()`` and
    ``_query(sql, max_rows)``, and call ``_confine_setup()`` once the schema is in use.
    Parsing runs on a connection of its own with no file attached (a parse reads no data),
    so the host may run its queries anywhere, a child process included.
    """

    #: Table functions that read neither the catalog, a file, nor SQL from a string.
    PLAIN_FUNCTIONS = frozenset({"range", "generate_series", "unnest", "json_each",
                                 "json_tree"})
    #: Table functions whose first argument names a table: allowed on this schema's tables.
    TABLE_ARGUMENT = frozenset({"pragma_table_info", "pragma_show", "pragma_storage_info"})
    #: Catalog table functions: answered for this schema only, or as they are (AS_IS).
    CATALOG_FUNCTIONS = frozenset({
        "duckdb_tables", "duckdb_views", "duckdb_columns", "duckdb_schemas",
        "duckdb_constraints", "duckdb_indexes", "duckdb_sequences", "duckdb_types",
        "duckdb_functions", "duckdb_dependencies", "duckdb_databases", "duckdb_settings",
        "duckdb_keywords", "duckdb_extensions", "duckdb_optimizers", "duckdb_memory",
        "duckdb_temporary_files", "duckdb_variables", "duckdb_approx_database_count",
        "duckdb_connection_count", "pragma_version", "pragma_platform", "pragma_user_agent",
        "pragma_collations", "pragma_database_size", "pragma_database_list"})
    #: Catalog sources that describe no schema of the warehouse.
    AS_IS = frozenset({
        "duckdb_databases", "duckdb_settings", "duckdb_keywords", "duckdb_extensions",
        "duckdb_optimizers", "duckdb_memory", "duckdb_temporary_files", "duckdb_variables",
        "duckdb_approx_database_count", "duckdb_connection_count", "pragma_version",
        "pragma_platform", "pragma_user_agent", "pragma_collations", "pragma_database_size",
        "pragma_database_list", "character_sets", "pg_database", "pg_settings", "pg_type"})
    #: Scalar functions that print catalog definitions.
    SCALAR_REFUSED = frozenset({"pg_get_viewdef", "pg_get_constraintdef", "pg_get_expr"})
    #: Where a catalog source names each row's schema and catalog, in order of preference.
    SCHEMA_COLUMNS = ("table_schema", "schema_name", "schema", "schemaname", "nspname",
                      "constraint_schema")
    CATALOG_COLUMNS = ("table_catalog", "database_name", "catalog_name", "database",
                       "constraint_catalog")
    #: Catalog columns holding SQL text; they read NULL.
    DEFINITION_COLUMNS = frozenset({"sql", "view_definition", "definition",
                                    "macro_definition", "indexdef", "check_clause"})
    #: Table-reference nodes that hold nothing to check themselves.
    PLAIN_REFS = frozenset({"SUBQUERY", "JOIN", "EXPRESSION_LIST", "EMPTY", "PIVOT"})
    EXPLAIN = re.compile(r"\s*(?:(?:--[^\n]*(?:\n|$)|/\*.*?\*/)\s*)*EXPLAIN\b"
                         r"(?:\s+ANALY[SZ]E\b)?(?:\s*\([^)]*\))?", re.I | re.S)
    DID_YOU_MEAN = re.compile(r"\s*Did you mean [^\n]*\?")

    def _confine_setup(self) -> None:
        import duckdb

        self._parser = duckdb.connect()   # in memory, nothing attached: parses only
        self._confine_scopes: dict[str, str | None] = {}
        self._confine_system: dict[str, set[str]] | None = None
        self._confine_siblings: set[str] = set()   # error hints are scrubbed against these
        table, _ = self._query(
            "SELECT schema_name FROM information_schema.schemata WHERE catalog_name = "
            "current_database() AND schema_name NOT IN ('information_schema', 'pg_catalog')",
            100_000)
        self._confine_siblings = ({str(s).lower() for s in table.column(0).to_pylist()}
                                  - {self.schema.lower()})

    # ------------------------------------------------------------------ the check
    def confine_statement(self, sql: str) -> str:
        """The statement to run, or `WarehouseError` saying why it cannot run here."""
        explain = self.EXPLAIN.match(sql)
        head, body = (sql[:explain.end()], sql[explain.end():]) if explain else ("", sql)
        tree = self._confine_parse(body)
        rewrites: list[tuple[dict, dict]] = []
        self._confine_walk(tree, self._cte_names(tree, set()), rewrites)
        if not rewrites:
            return sql
        for node, replacement in rewrites:
            node.clear()
            node.update(replacement)
        import duckdb
        try:
            (text,) = self._parser.execute("SELECT json_deserialize_sql(?)",
                                           [json.dumps(tree)]).fetchone()
        except duckdb.Error as exc:
            raise WarehouseError(f"this catalog query could not be answered for "
                                 f"{self.schema}; list_tables and describe_table describe "
                                 f"it") from exc
        return f"{head} {text}" if head else text

    def _confine_refuse(self, named) -> None:
        named = sorted({str(n) for n in named if n})
        raise WarehouseError(
            f"this warehouse is {self.schema} and reads nothing else; the statement also "
            f"names {', '.join(named) or 'another schema'}. Query the tables in "
            f"{self.schema}.")

    def _confine_parse(self, sql: str) -> dict:
        import duckdb

        (raw,) = self._parser.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()
        tree = json.loads(raw)
        if not tree.get("error"):
            return tree
        try:   # DuckDB's own message and pointer; parsing reads nothing
            self._parser.extract_statements(sql)
        except duckdb.Error as exc:
            raise WarehouseError(str(exc)) from exc
        raise WarehouseError(f"this statement could not be checked against {self.schema} "
                             f"({tree.get('error_message')}); write it as a single SELECT")

    @staticmethod
    def _cte_names(node: Any, found: set[str]) -> set[str]:
        if isinstance(node, dict):
            for entry in (node.get("cte_map") or {}).get("map") or []:
                found.add(str(entry.get("key", "")).lower())
            for value in node.values():
                DuckDBConfinement._cte_names(value, found)
        elif isinstance(node, list):
            for value in node:
                DuckDBConfinement._cte_names(value, found)
        return found

    @staticmethod
    def _quoted(name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    @staticmethod
    def _literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    def _system_views(self) -> dict[str, set[str]]:
        """DuckDB's catalog views by lower-cased name, with the schemas defining each."""
        if self._confine_system is None:
            table, _ = self._query("SELECT schema_name, view_name FROM duckdb_views() "
                                   "WHERE database_name = 'system'", 10_000)
            found: dict[str, set[str]] = {}
            for schema, name in zip(*(table.column(i).to_pylist() for i in range(2))):
                found.setdefault(str(name).lower(), set()).add(str(schema).lower())
            self._confine_system = found
        return self._confine_system

    def _own_tables(self) -> set[str]:
        return {t.lower() for t in self.list_tables()}

    def _no_table(self, name: str) -> WarehouseError:
        tables = {t.upper(): t for t in self.list_tables()}
        close = difflib.get_close_matches(name.upper(), list(tables), n=1)
        hint = f'\nDid you mean "{tables[close[0]]}"?' if close else ""
        return WarehouseError(f"Catalog Error: Table with name {name} does not exist!{hint}")

    def _confine_walk(self, node: Any, ctes: set[str], rewrites: list) -> None:
        if isinstance(node, list):
            for value in node:
                self._confine_walk(value, ctes, rewrites)
            return
        if not isinstance(node, dict):
            return
        kind = node.get("type")
        if node.get("class") == "FUNCTION":
            self._check_scalar(node)
        elif ("class" not in node and isinstance(kind, str) and not kind.endswith("_NODE")
              and "sample" in node):
            if self._check_ref(node, ctes, rewrites):
                return   # replaced by a scoped catalog query
        for value in node.values():
            self._confine_walk(value, ctes, rewrites)

    def _check_scalar(self, node: dict) -> None:
        name = str(node.get("function_name") or "").lower()
        if name in self.SCALAR_REFUSED:
            raise WarehouseError(f"{name}() is not available here")
        schema = str(node.get("schema") or "").lower()
        if schema not in ("", "main", "pg_catalog", self.schema.lower()):
            self._confine_refuse([schema])

    def _check_ref(self, node: dict, ctes: set[str], rewrites: list) -> bool:
        kind = node["type"]
        if kind in self.PLAIN_REFS:
            return False
        if kind == "BASE_TABLE":
            return self._check_table(node, ctes, rewrites)
        if kind == "TABLE_FUNCTION":
            return self._check_table_function(node, rewrites)
        if kind == "SHOW_REF":
            return self._check_show(node, rewrites)
        raise WarehouseError(f"this statement reads a {kind} source, which the warehouse "
                             f"cannot check; query the tables in {self.schema}")

    def _check_table(self, node: dict, ctes: set[str], rewrites: list) -> bool:
        catalog = str(node.get("catalog_name") or "").lower()
        schema = str(node.get("schema_name") or "").lower()
        name = str(node.get("table_name") or "")
        low, system = name.lower(), self._system_views()
        if not catalog and not schema:
            if low in ctes:
                if low in system:
                    raise WarehouseError(f"a CTE here cannot be called {name}, the name of a "
                                         f"DuckDB catalog view; rename it")
                return False
            if low in self._own_tables():
                return False
            if system.get(low, set()) & {"main", "pg_catalog"}:
                return self._scope(node, f"SELECT * FROM {self._quoted(name)}", low, name,
                                   rewrites)
            raise self._no_table(name)
        if catalog in ("", "warehouse") and schema == self.schema.lower():
            if low in self._own_tables():
                return False
            raise self._no_table(name)
        if schema in system.get(low, set()) and (
                (schema in ("information_schema", "pg_catalog")
                 and catalog in ("", "system", "warehouse"))
                or (schema == "main" and catalog == "system")):
            return self._scope(node, f"SELECT * FROM {self._quoted(schema)}."
                                     f"{self._quoted(name)}", f"{schema}.{low}", name, rewrites)
        self._confine_refuse([schema or catalog]
                             + ([catalog] if schema and catalog not in ("", "warehouse") else []))
        return False

    def _check_table_function(self, node: dict, rewrites: list) -> bool:
        fn = node.get("function") or {}
        name = str(fn.get("function_name") or "").lower()
        schema = str(fn.get("schema") or "").lower()
        catalog = str(fn.get("catalog") or "").lower()
        if schema not in ("", "main", "pg_catalog") or catalog not in ("", "system"):
            self._confine_refuse([schema or catalog])
        args = fn.get("children") or []
        if name in self.PLAIN_FUNCTIONS:
            return False
        if name in self.TABLE_ARGUMENT:
            first = args[0] if args else {}
            value = ((first.get("value") or {}).get("value")
                     if first.get("class") == "CONSTANT" else None)
            if not isinstance(value, str):
                raise WarehouseError(f"{name}() here takes a table name in quotes")
            self._confine_walk(self._confine_parse(f"SELECT * FROM {value}"), set(), [])
            return False
        if name in self.CATALOG_FUNCTIONS:
            if args:
                raise WarehouseError(f"{name}() takes no arguments here")
            return self._scope(node, f"SELECT * FROM {name}()", name, name, rewrites)
        raise WarehouseError(
            f"{name}() is not available here: the warehouse answers queries on the tables of "
            f"{self.schema}, not SQL in strings, files or other schemas. Query the tables "
            f"directly.")

    def _check_show(self, node: dict, rewrites: list) -> bool:
        kind = node.get("show_type")
        if kind in ("DESCRIBE", "SUMMARY"):
            return False   # the described query is checked like any other
        if kind == "SHOW_FROM":
            schema = str(node.get("schema_name") or "").lower()
            catalog = str(node.get("catalog_name") or "").lower()
            if schema == self.schema.lower() and catalog in ("", "warehouse"):
                return False
            self._confine_refuse([schema or catalog])
        if kind == "SHOW_UNQUALIFIED":
            what = str(node.get("table_name") or "").strip('"').lower()
            if what in ("tables", "databases"):
                return False
            if what == "schemas":
                return self._scope(node, "SELECT * FROM (SHOW SCHEMAS)", "show schemas",
                                   "schemas", rewrites)
            if what == "__show_tables_expanded":
                return self._scope(node, "SELECT * FROM (SHOW ALL TABLES)", "show all tables",
                                   "tables", rewrites)
        raise WarehouseError("this SHOW form is not available here; SHOW TABLES and "
                             "DESCRIBE <table> are")

    def _scope(self, node: dict, inner: str, key: str, alias: str, rewrites: list) -> bool:
        """Queue ``node`` (a catalog source) to be replaced by ``inner`` narrowed to this
        schema. False when the source describes no schema and runs as written."""
        scoped = self._scoped_sql(key, inner)
        if scoped is None:
            return False
        tree = self._confine_parse(
            f"SELECT * FROM ({scoped}) AS {self._quoted(node.get('alias') or alias)}")
        replacement = tree["statements"][0]["node"]["from_table"]
        replacement["column_name_alias"] = node.get("column_name_alias") or []
        rewrites.append((node, replacement))
        return True

    def _scoped_sql(self, key: str, inner: str) -> str | None:
        if key in self._confine_scopes:
            return self._confine_scopes[key]
        if key.rsplit(".", 1)[-1] in self.AS_IS:
            self._confine_scopes[key] = None
            return None
        columns = [str(c) for c in self._query(f"DESCRIBE {inner}", 10_000)[0]
                   .column(0).to_pylist()]
        by_lower = {c.lower(): c for c in columns}
        schema_col = next((by_lower[c] for c in self.SCHEMA_COLUMNS if c in by_lower), None)
        if schema_col is None:
            raise WarehouseError(
                f"{key} is not available here; information_schema.tables and .columns, "
                f"SHOW TABLES and DESCRIBE <table> describe {self.schema}")
        catalog_col = next((by_lower[c] for c in self.CATALOG_COLUMNS if c in by_lower), None)
        ours = self._literal(self.schema.lower())
        sch = f"lower(coalesce({self._quoted(schema_col)}, ''))"
        if catalog_col:
            keep = (f"NOT (lower(coalesce({self._quoted(catalog_col)}, '')) = 'warehouse' "
                    f"AND {sch} <> {ours})")
        else:
            others = ", ".join(self._literal(s) for s in sorted(self._confine_siblings))
            keep = f"{sch} NOT IN ({others or self._literal('')})"
        blanks = [f"CAST(NULL AS VARCHAR) AS {self._quoted(c)}" for c in columns
                  if c.lower() in self.DEFINITION_COLUMNS]
        star = f"* REPLACE ({', '.join(blanks)})" if blanks else "*"
        self._confine_scopes[key] = f"SELECT {star} FROM ({inner}) WHERE {keep}"
        return self._confine_scopes[key]

    def _scrub(self, message: str) -> str:
        """DuckDB's error with any "Did you mean" hint that names another schema dropped."""
        others = getattr(self, "_confine_siblings", set())

        def drop(m: re.Match) -> str:
            text = m.group(0).lower()
            named = any(re.search(rf"(?<![\w$]){re.escape(o)}(?![\w$])", text) for o in others)
            return "" if named else m.group(0)
        return self.DID_YOU_MEAN.sub(drop, message)


# ---------------------------------------------------------------------------- DuckDB
#: Seconds a statement may run past its limit before its query process is killed. An
#: interrupted statement returns within a second or so; only a wedged one is still going.
KILL_GRACE_S = 30.0
#: Seconds a new query process may take to attach the file.
START_TIMEOUT_S = 300.0


def _duckdb_connect(path: str, schema: str, spill: str):
    """The file attached read-only, the schema in use, the host's filesystem fenced off.
    Spills go to ``spill``, a directory of this process's own: an in-memory connection
    otherwise spills to ``./.tmp``, which two processes in one directory would share."""
    import duckdb

    conn = duckdb.connect()
    conn.execute(f"SET temp_directory = {_sql_literal(spill)}")
    conn.execute("SET enable_progress_bar = false")
    conn.execute(f"ATTACH {_sql_literal(path)} AS warehouse (READ_ONLY)")
    conn.execute(f'USE warehouse."{schema}"')
    conn.execute(f"SET allowed_paths = [{_sql_literal(path)}]")
    conn.execute("SET enable_external_access = false")
    conn.execute("SET lock_configuration = true")
    return conn


def _duckdb_fetch(conn, sql: str, max_rows: int, timeout_s: float) -> tuple[pa.Table, bool]:
    """One statement, materialized: at most ``max_rows`` + 1 rows are kept, which is how a
    truncated result shows. Not streamed: DuckDB 1.4 and 1.5 can deadlock a streamed result
    whose plan unions a scan with a join (duckdb/duckdb#26408), where no interrupt reaches."""
    import duckdb

    timer = threading.Timer(timeout_s, conn.interrupt)
    timer.start()
    try:
        relation = conn.sql(sql)
        if relation is None:
            return pa.table({}), False
        table = relation.limit(max_rows + 1).to_arrow_table()
    except duckdb.InterruptException as exc:
        raise WarehouseError(f"query exceeded {timeout_s:.0f}s and was interrupted") from exc
    except duckdb.Error as exc:
        raise WarehouseError(str(exc)) from exc
    finally:
        timer.cancel()
    table = table.rename_columns(_unique_names(table.column_names))
    return table.slice(0, max_rows), table.num_rows > max_rows


#: How a query process starts: it loads this file (by path, so any copy of it works) and
#: runs `_duckdb_child` on the socket it inherits.
_DUCKDB_BOOT = ("import importlib.util, sys\n"
                "spec = importlib.util.spec_from_file_location('argo_duckdb_child', sys.argv[1])\n"
                "module = sys.modules[spec.name] = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(module)\n"
                "module._duckdb_child(int(sys.argv[2]), sys.argv[3])\n")


class _PipeFile:
    """A write-only file whose every write goes out as one message on a Connection: what
    the query process streams a result through. An empty message ends the stream."""

    closed = False

    def __init__(self, pipe) -> None:
        self._pipe = pipe

    def write(self, data) -> int:
        if len(data):
            self._pipe.send_bytes(data)
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def writable(self) -> bool:
        return True


class _PipeReader:
    """A read-only file over the pieces `_PipeFile` sends; the empty message is its end."""

    closed = False

    def __init__(self, pipe, timeout_s: float) -> None:
        self._pipe, self._timeout_s = pipe, timeout_s
        self._piece, self._at, self._done = b"", 0, False

    def read(self, size: int = -1) -> bytes:
        out = []
        while (size < 0 or size > 0) and not self._done:
            if self._at == len(self._piece):
                if not self._pipe.poll(self._timeout_s):
                    raise TimeoutError("the query process stopped sending its result")
                self._piece, self._at = self._pipe.recv_bytes(), 0
                if not self._piece:
                    self._done = True
                    break
            take = len(self._piece) - self._at if size < 0 else min(size, len(self._piece) - self._at)
            out.append(self._piece[self._at:self._at + take])
            self._at += take
            if size > 0:
                size -= take
        return b"".join(out)

    def drain(self) -> None:
        """Read to the end message, so the next reply starts clean."""
        while not self._done:
            self._at = len(self._piece)
            self.read(1)

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        self.closed = True


def _duckdb_child(fd: int, spec: str) -> None:
    """The query process: one connection, one statement at a time, each result sent back as
    an Arrow stream over the socket ``fd``."""
    import json
    from multiprocessing.connection import Connection

    pipe = Connection(fd)
    spec = json.loads(spec)
    timeout_s = spec["timeout_s"]
    try:
        conn = _duckdb_connect(spec["path"], spec["schema"], spec["spill"])
    except Exception as exc:  # noqa: BLE001 -- reported to the parent, which fails to open
        pipe.send(("error", f"cannot open the DuckDB warehouse: {exc}"))
        return
    pipe.send(("ready", None))
    while True:
        try:
            sql, max_rows = pipe.recv()
        except EOFError:
            return
        try:
            table, truncated = _duckdb_fetch(conn, sql, max_rows, timeout_s)
        except WarehouseError as exc:
            pipe.send(("error", str(exc)))
            continue
        # The result goes out as an Arrow stream in pieces, so neither side holds a second
        # whole copy of it (a serialized buffer, its bytes, a pickle) beside the table.
        pipe.send(("ok", truncated))
        with pa.ipc.new_stream(pa.PythonFile(_PipeFile(pipe), mode="w"), table.schema) as writer:
            writer.write_table(table, max_chunksize=65_536)
        pipe.send_bytes(b"")


class DuckDBWarehouse(DuckDBConfinement, Warehouse):
    """A DuckDB file attached read-only with the filesystem fenced off: the agent's SQL
    can read the warehouse and nothing else on the host.

    The connection lives in a query process of its own. A statement past its limit is
    interrupted there. One the interrupt cannot reach (a wedged engine), or one that brings
    the process down (out of memory, a crash), costs that process and not the warehouse: it
    is killed and replaced, and the caller gets an error rather than a warehouse that never
    answers again."""

    engine = "duckdb"
    dialect = "DuckDB SQL"
    lexicon = Lexicon(dollar_quotes=True)

    def __init__(self, *, database: str, schema: str, timeout_s: float = DEFAULT_TIMEOUT_S,
                 **_: Any) -> None:
        super().__init__(database=database, schema=schema, timeout_s=timeout_s)
        path = Path(database).expanduser().resolve()
        if not path.is_file():
            raise WarehouseError(f"no DuckDB warehouse at {path} (scripts/load_duckdb.py)")
        self._path = str(path)
        self._proc = self._pipe = self._spill = None
        self._start()
        self._confine_setup()

    def confine(self, sql: str) -> str:
        return self.confine_statement(sql)

    def _start(self) -> None:
        import json
        import socket
        import subprocess
        import sys
        import tempfile
        from multiprocessing.connection import Connection

        self._spill = tempfile.mkdtemp(prefix="argo-duckdb-")
        ours, theirs = socket.socketpair()
        spec = json.dumps({"path": self._path, "schema": self.schema, "spill": self._spill, "timeout_s": self.timeout_s})
        proc = subprocess.Popen([sys.executable, "-c", _DUCKDB_BOOT, __file__,
                                 str(theirs.fileno()), spec], pass_fds=(theirs.fileno(),),
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        theirs.close()
        pipe = Connection(ours.detach())
        try:
            reply = pipe.recv() if pipe.poll(START_TIMEOUT_S) else ("error", "it did not open in time")
        except EOFError:
            reply = ("error", f"its query process exited ({proc.poll()})")
        self._proc, self._pipe = proc, pipe
        if reply[0] != "ready":
            self._stop()
            raise WarehouseError(f"the DuckDB warehouse did not open: {reply[1]}")

    def _stop(self) -> None:
        import shutil

        if self._proc is not None:
            self._proc.kill()
            self._proc.wait(timeout=30)
            self._pipe.close()
        if self._spill:
            shutil.rmtree(self._spill, ignore_errors=True)
        self._proc = self._pipe = self._spill = None

    def _restart(self) -> None:
        self._stop()
        self._start()

    def _query(self, sql: str, max_rows: int) -> tuple[pa.Table, bool]:
        if self._proc is None or self._proc.poll() is not None:
            self._restart()
        self._pipe.send((sql, max_rows))
        if not self._pipe.poll(self.timeout_s + KILL_GRACE_S):
            self._restart()
            raise WarehouseError(f"query exceeded {self.timeout_s:.0f}s and was interrupted")
        try:
            reply = self._pipe.recv()
            if reply[0] == "ok":
                # Read straight off the socket: each piece is dropped once Arrow has it.
                source = _PipeReader(self._pipe, self.timeout_s + KILL_GRACE_S)
                try:
                    table = pa.ipc.open_stream(pa.PythonFile(source, mode="r")).read_all()
                    source.drain()   # Arrow stops at its own end mark, before ours
                except TimeoutError:
                    self._restart()
                    raise WarehouseError(f"query exceeded {self.timeout_s:.0f}s and was "
                                         f"interrupted") from None
        except EOFError:
            self._restart()
            raise WarehouseError("the statement brought down the warehouse's query process "
                                 "(most likely it ran out of memory); the warehouse has "
                                 "restarted, and the statement returned nothing") from None
        if reply[0] == "error":
            raise WarehouseError(self._scrub(reply[1]))
        return table, reply[1]

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
        self._stop()


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
