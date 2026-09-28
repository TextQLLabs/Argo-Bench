#!/usr/bin/env python3
"""Load the released warehouse into one local DuckDB file, with its as-of month views.

    python scripts/load_duckdb.py --kit path/to/argo-bench --out data/argo.duckdb

``--kit`` is a copy of the data release: the published dataset (``data/<TABLE>/*.parquet``
and ``setup/duckdb/month_views.sql``) or the release kit (``warehouse/<TABLE>.parquet``
and ``setup/month_views/ducklake.sql``). The tables land in schema ``food_delivery`` and
the eleven as-of months beside it as ``food_delivery_1`` .. ``food_delivery_11``
(December is the base).
"""

from __future__ import annotations

import argparse
from pathlib import Path

BASE = "food_delivery"
TABLE_DIRS = ("data", "warehouse")
VIEW_FILES = ("setup/duckdb/month_views.sql", "setup/month_views/ducklake.sql",
              "setup/month_views/duckdb.sql")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--kit", required=True, type=Path)
    ap.add_argument("--out", default=Path("data/argo.duckdb"), type=Path)
    args = ap.parse_args()

    import duckdb

    warehouse = next((args.kit / d for d in TABLE_DIRS if (args.kit / d).is_dir()),
                     args.kit / TABLE_DIRS[0])
    tables = sorted({p.stem if p.is_file() else p.name for p in warehouse.iterdir()
                     if p.suffix == ".parquet" or p.is_dir()}) if warehouse.is_dir() else []
    if not tables:
        raise SystemExit(f"no Parquet tables under {warehouse}")
    views = next((args.kit / f for f in VIEW_FILES if (args.kit / f).is_file()), None)
    if views is None:
        raise SystemExit(f"none of {VIEW_FILES} under {args.kit}: "
                         "the as-of questions need the month views")
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
