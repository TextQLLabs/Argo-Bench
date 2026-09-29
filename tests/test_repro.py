"""Offline checks of the pieces a run depends on: no model, no API key, no data release."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from argo_bench.prompt import dataset_for, system_prompt  # noqa: E402
from argo_bench.servers.python import KERNEL, Kernel, SyncedKernel  # noqa: E402
from argo_bench.task import load_cards  # noqa: E402
from argo_bench.warehouse import (ReadOnlyViolation, WarehouseError,  # noqa: E402
                                  open_warehouse, read_only_statement)


def test_cards():
    cards = load_cards("final")
    assert len({c["id"] for c in cards}) == len(cards) > 200
    for card in cards:
        assert card["prompt"].strip() and card["data_cutoff"].startswith("2024-")
    assert load_cards("smoke")[0]["expected"]


def test_as_of_questions_read_their_month():
    from argo_bench.agent import question_view
    from argo_bench.task import argo_bench

    samples = {s.id: s for s in argo_bench(questions="final").dataset}
    november = next(s for s in samples.values() if s.metadata["data_cutoff"] == "2024-11")
    dataset, system = question_view(november.metadata, "duckdb", "food_delivery")
    assert dataset == "food_delivery_11" and "end of November 2024" in system
    december = next(s for s in samples.values() if s.metadata["data_cutoff"] == "2024-12")
    assert question_view(december.metadata, "duckdb", "food_delivery")[0] == "food_delivery"


def test_months():
    assert dataset_for("food_delivery", "2024-09") == "food_delivery_9"
    assert dataset_for("food_delivery", "2024-12") == "food_delivery"
    assert "end of September 2024" in system_prompt("duckdb", "2024-09")
    assert "the calendar year 2024," in system_prompt("duckdb", "2024-12")


def test_read_only_guard():
    assert read_only_statement("SELECT 1;") == "SELECT 1"
    for sql in ("DELETE FROM t", "SELECT 1; SELECT 2", "WITH x AS (DELETE FROM t) SELECT 1"):
        with pytest.raises(ReadOnlyViolation):
            read_only_statement(sql)
    assert read_only_statement("SELECT 'drop table' AS note")


def test_duckdb_month_confinement(tmp_path):
    path = tmp_path / "w.duckdb"
    with duckdb.connect(str(path)) as con:
        con.execute("CREATE SCHEMA food_delivery")
        con.execute("CREATE TABLE food_delivery.ORDERS AS SELECT range AS id, "
                    "DATE '2024-01-01' + range::INT AS day FROM range(366)")
        con.execute("CREATE SCHEMA food_delivery_9")
        con.execute("CREATE VIEW food_delivery_9.ORDERS AS SELECT * FROM food_delivery.ORDERS "
                    "WHERE day < DATE '2024-10-01'")
    wh = open_warehouse("duckdb", database=str(path), schema="food_delivery_9")
    assert wh.list_tables() == ["ORDERS"]
    assert wh.run_sql("SELECT COUNT(*) AS n FROM ORDERS").table.column(0)[0].as_py() == 274
    with pytest.raises(WarehouseError, match="reads nothing else"):
        wh.run_sql("SELECT COUNT(*) FROM food_delivery.ORDERS")
    with pytest.raises(WarehouseError):
        wh.run_sql("SELECT * FROM read_csv('/etc/passwd')")


def test_local_kernel_keeps_state(tmp_path):
    kernel = Kernel([sys.executable, str(KERNEL)], tmp_path, {"KERNEL_LOG": str(tmp_path / "k")},
                    30, 30_000)
    try:
        assert kernel.execute("x = 41") == "(no output)"
        assert kernel.execute("x + 1").strip() == "42"
        assert "ZeroDivisionError" in kernel.execute("1/0")
    finally:
        kernel.close()


def test_synced_kernel_moves_files_and_journal(tmp_path):
    """The docker/k8s transport, with a plain process standing in for the container."""
    host, box = tmp_path / "host", tmp_path / "box"
    host.mkdir(), box.mkdir()
    (host / "results").mkdir()
    (host / "results" / "sql_0001.txt").write_text("hello")
    launch = lambda name: ["/bin/sh", "-c", f"cd '{box}' && exec '{sys.executable}' '{KERNEL}'"]  # noqa: E731
    kernel = SyncedKernel(host, {"KERNEL_LOG": str(tmp_path / "k")}, 30, 30_000, "t",
                          launch=launch, stop=lambda name: None)
    try:
        assert kernel.execute("print(open('results/sql_0001.txt').read())").strip() == "hello"
        kernel.execute("open('mission_control_actions.jsonl', 'a').write('{\"kind\": \"note\"}\\n')")
        assert json.loads((host / "mission_control_actions.jsonl").read_text()) == {"kind": "note"}
        assert not (host / "kernel.log").exists()
    finally:
        kernel.close()


def test_console_files_offline(tmp_path):
    for name in ("mission_control.py", "mission_control_plumbing.py"):
        (tmp_path / name).write_bytes((REPO / "argo_bench" / "console" / name).read_bytes())
    code = ("from mission_control import MissionControl, Reason\n"
            "mc = MissionControl()\n"
            "mc.ban_customers([101, 102], reason=Reason.PROMO_FARMING)\n"
            "print(mc.summary())\n")
    done = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True,
                          text=True, check=True)
    assert "2 decision(s)" in done.stdout
    assert json.loads((tmp_path / "mission_control_actions.jsonl").read_text())["kind"] == \
        "ban_customers"


def test_synced_kernel_timeout_replaces_the_container(tmp_path):
    host, box = tmp_path / "host", tmp_path / "box"
    host.mkdir(), box.mkdir()
    (host / "data.txt").write_text("x")
    stopped = []
    launch = lambda name: ["/bin/sh", "-c", f"cd '{box}' && exec '{sys.executable}' '{KERNEL}'"]  # noqa: E731
    kernel = SyncedKernel(host, {"KERNEL_LOG": str(tmp_path / "k")}, 2, 30_000, "t",
                          launch=launch, stop=stopped.append)
    try:
        assert "Timed out" in kernel.execute("import time; time.sleep(10)")
        assert stopped == ["t-1"]
        (box / "data.txt").unlink()   # a new container starts empty: everything is re-sent
        reply = kernel.execute("print(open('data.txt').read())")
        assert reply.startswith("[the previous interpreter is gone") and reply.strip().endswith("x")
        assert kernel.name == "t-2"
    finally:
        kernel.close()


def test_reference_modules():
    """Every reference solution names a question, and each of its calls is well formed on
    both engines: a known tool, a single read-only statement, Python that compiles."""
    from argo_bench import reference
    from argo_bench.warehouse import BigQueryWarehouse, DuckDBWarehouse

    cards = {c["id"]: c for c in load_cards("final") + load_cards("smoke")}
    questions = reference.available()
    assert len(questions) >= 20 and "smoke-01-console" in questions
    lexicons = {"bigquery": BigQueryWarehouse.lexicon, "duckdb": DuckDBWarehouse.lexicon}
    for question in questions:
        module = reference.load(question)
        assert reference.module_path(module.QUESTION).is_file(), question
        assert module.__doc__ and "Score:" in module.__doc__, question
        for engine, lexicon in lexicons.items():
            steps = reference.steps(module, cards[question]["prompt"], engine)
            assert steps and steps[-1][0] == "run_python", question
            for tool, arguments in steps:
                if tool == "run_sql":
                    read_only_statement(arguments["sql"], lexicon)
                elif tool == "run_python":
                    compile(arguments["code"], f"{question} (run_python)", "exec")


def test_reference_replays_the_smoke_question(tmp_path):
    """The reference solver end to end, through both MCP servers, with no model and no data."""
    from inspect_ai import eval as inspect_eval

    from argo_bench.task import argo_bench

    log = inspect_eval(argo_bench(questions="smoke", solver="reference"), model="mockllm/model",
                       log_dir=str(tmp_path), display="none")[0]
    assert log.status == "success"
    scores = {s.name: s.metrics["mean"].value for s in log.results.scores}
    assert scores == {"filed": 1.0, "conformance": 1.0}
