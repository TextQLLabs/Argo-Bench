#!/usr/bin/env python3
"""Load the released warehouse into one local DuckDB file, with its as-of month views.

    python scripts/load_duckdb.py --kit path/to/release --out data/argo.duckdb

``--kit`` is the unpacked data release: ``warehouse/<TABLE>.parquet`` (or a directory of
Parquet per table) and ``setup/month_views/duckdb.sql``. The tables land in schema
``food_delivery`` and the eleven as-of months beside it as ``food_delivery_1`` ..
``food_delivery_11`` (December is the base).
"""

from __future__ import annotations

import argparse
from pathlib import Path

BASE = "food_delivery"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--kit", required=True, type=Path)
    ap.add_argument("--out", default=Path("data/argo.duckdb"), type=Path)
    args = ap.parse_args()

    import duckdb

    warehouse = args.kit / "warehouse"
    tables = sorted({p.stem if p.is_file() else p.name for p in warehouse.iterdir()
                     if p.suffix == ".parquet" or p.is_dir()})
    if not tables:
        raise SystemExit(f"no Parquet tables under {warehouse}")
    views = args.kit / "setup" / "month_views" / "duckdb.sql"
    if not views.is_file():
        raise SystemExit(f"{views} not found: the as-of questions need the month views")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(args.out))
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {BASE}")
    for table in tables:
        source = warehouse / f"{table}.parquet"
        glob = str(source) if source.is_file() else str(warehouse / table / "*.parquet")
        con.execute(f'CREATE OR REPLACE TABLE {BASE}."{table}" AS '
                    f"SELECT * FROM read_parquet('{glob}')")
        print(f"  {table}")
    con.execute(views.read_text(encoding="utf-8"))
    con.close()
    print(f"loaded {len(tables)} table(s) and month views {BASE}_1 .. {BASE}_11 into {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
