"""How warehouse-like is Spider 2.0? Provenance, sharding and tables read.

Reads a checkout of github.com/xlang-ai/Spider2 and writes every number the
Argo-Bench appendix quotes about it to one JSON file.

    git clone --filter=blob:none https://github.com/xlang-ai/Spider2
    git -C Spider2 checkout cafb867313aab4e674652054198f383cf4018943
    pip install sqlglot
    python spider2_shape.py --spider2 Spider2 --out spider2_shape.json

Three measurements, all over Spider 2.0-Snow (547 tasks, 152 databases):

1. Provenance. Each task is attributed to where its database comes from. Task-id
   prefixes give the host (``sf_bq``/``sf_ga`` = a BigQuery dataset re-hosted on
   Snowflake, ``sf_local`` = a local SQLite file, ``sf`` = Snowflake Marketplace);
   PROVENANCE below records the databases whose origin needed checking by hand,
   with the source that settles each.
2. Sharding. A *schema family* is the set of tables in one database whose
   (column name, type) lists are identical: the date, geography or version shards
   of one logical table. Tables whose DDL is empty are skipped; they are names the
   Cybersyn listings advertise but do not share.
3. Tables read. Every table a gold query reads, from two independent sources:
   the gold SQL released for about half the tasks (parsed with sqlglot, every
   reference resolved against the task's own database) and the authors'
   ``methods/gold-tables`` lists, which cover all 547. Shards collapse to one
   *logical table* when their names agree once digit runs are masked
   (``ga_sessions_20170801`` -> ``ga_sessions_#``); a ``_*`` wildcard is one
   logical table.
"""

from __future__ import annotations

import argparse
import collections
import csv
import glob
import json
import os
import re
import statistics
import subprocess

import sqlglot
from sqlglot import exp

csv.field_size_limit(1 << 30)

# ---------------------------------------------------------------------------
# Provenance. Classes, in the order the appendix table lists them.
BQ_GOOGLE = "BigQuery public data (Google)"
BQ_THIRD = "BigQuery public data (third party)"
SF_MARKET = "Snowflake Marketplace"
LOCAL = "Local SQLite files"
OTHER = "Other public dumps re-hosted on Snowflake"

# Databases under the sf_bq / sf_ga prefixes that do not come from Google's
# public-data program. Everything else under those prefixes is
# bigquery-public-data.<dataset> (schema names match one-to-one).
BQ_NOT_GOOGLE = {
    "CPTAC_PDC": (BQ_THIRD, "isb-cgc-bq (NCI Cancer Research Data Commons)"),
    "HTAN_1": (BQ_THIRD, "isb-cgc-bq.HTAN_versioned"),
    "HTAN_2": (BQ_THIRD, "isb-cgc-bq.HTAN"),
    "TCGA": (BQ_THIRD, "isb-cgc-bq.TCGA_versioned"),
    "TCGA_BIOCLIN_V0": (BQ_THIRD, "isb-cgc.TCGA_bioclin_v0"),
    "TCGA_HG19_DATA_V0": (BQ_THIRD, "isb-cgc.TCGA_hg19_data_v0"),
    "TCGA_HG38_DATA_V0": (BQ_THIRD, "isb-cgc.TCGA_hg38_data_v0"),
    "TCGA_MITELMAN": (BQ_THIRD, "isb-cgc-bq + mitelman-db.prod"),
    "PANCANCER_ATLAS_1": (BQ_THIRD, "isb-cgc-bq.pancancer_atlas"),
    "PANCANCER_ATLAS_2": (BQ_THIRD, "isb-cgc-bq.pancancer_atlas"),
    "TARGETOME_REACTOME": (BQ_THIRD, "isb-cgc-bq reactome/targetome"),
    "MITELMAN": (BQ_THIRD, "mitelman-db.prod"),
    "OPEN_TARGETS_GENETICS_1": (BQ_THIRD, "open-targets-genetics.genetics"),
    "OPEN_TARGETS_PLATFORM_1": (BQ_THIRD, "open-targets-prod.platform"),
    "ECOMMERCE": (BQ_GOOGLE, "data-to-insights.ecommerce (Google training project)"),
    "NCAA_INSIGHTS": (BQ_GOOGLE, "data-to-insights.ncaa (Google training project)"),
    "FIREBASE": (BQ_GOOGLE, "firebase-public-project (Google demo app)"),
    "GITHUB_REPOS_DATE": (BQ_THIRD, "githubarchive.{day,month,year} (GH Archive)"),
    "PATENTSVIEW": (BQ_GOOGLE, "patents-public-data.patentsview"),
    "PATENTS_GOOGLE": (BQ_GOOGLE, "patents-public-data.google_patents_research"),
    "PATENTS_USPTO": (BQ_GOOGLE, "bigquery-public-data.patents + patents-public-data.uspto_*"),
    "_1000_GENOMES": (BQ_GOOGLE, "Google Genomics 1000 Genomes (authors' copy)"),
    "META_KAGGLE": (OTHER, "Kaggle Meta Kaggle"),
    "WIDE_WORLD_IMPORTERS": (OTHER, "Microsoft WideWorldImporters sample"),
    "DEATH": (OTHER, "CDC NVSS mortality microdata (Kaggle packaging)"),
    "WORD_VECTORS_US": (OTHER, "author-assembled: article corpus + GloVe vectors"),
}

# Databases that are synthetic, obfuscated, or a vendor or textbook sample.
SYNTHETIC = {
    "THELOOK_ECOMMERCE": "Looker's synthetic theLook store",
    "GA4": "obfuscated GA4 Merchandise Store sample",
    "GA360": "obfuscated GA360 Merchandise Store sample",
    "ECOMMERCE": "obfuscated GA360 copy (data-to-insights)",
    "FIREBASE": "Firebase demo app export, 50,000 rows a day",
    "CYMBAL_INVESTMENTS": "Google Cymbal fictional-brand trades",
    "BRAZE_USER_EVENT_DEMO_DATASET": "synthetic Braze event stream",
    "FHIR_SYNTHEA": "Synthea synthetic patients",
    "SQLITE_SAKILA": "MySQL Sakila sample",
    "PAGILA": "Sakila, PostgreSQL port",
    "NORTHWIND": "Microsoft Northwind sample",
    "ADVENTUREWORKS": "Microsoft AdventureWorks sample",
    "WIDE_WORLD_IMPORTERS": "Microsoft WideWorldImporters sample",
    "CHINOOK": "Chinook sample",
    "MUSIC": "Chinook sample",
    "COMPLEX_ORACLE": "Oracle SH sample schema",
    "ORACLE_SQL": "Practical Oracle SQL (Apress) book schema",
    "BOWLINGLEAGUE": "SQL Queries for Mere Mortals sample",
    "ENTERTAINMENTAGENCY": "SQL Queries for Mere Mortals sample",
    "SCHOOL_SCHEDULING": "SQL Queries for Mere Mortals sample",
    "AIRLINES": "Postgres Pro generated demo database",
    "LOG": "SQL Recipes (Mynavi) book sample",
}
# Databases that mix real data with a synthetic teaching set: an upper bound.
PARTLY_SYNTHETIC = {
    "BANK_SALES_TRADING": "8 Week SQL Challenge cases + one real set",
    "CITY_LEGISLATION": "includes a Mockaroo set",
    "MODERN_DATA": "includes the Pizza Runner case",
    "CMS_DATA": "includes CMS synthetic OMOP patients",
    "EDUCATION_BUSINESS": "AtliQ Hardware + Parch & Posey teaching sets",
}


def provenance(instance_id: str, db: str) -> str:
    if instance_id.startswith("sf_local"):
        return LOCAL
    if instance_id.startswith(("sf_bq", "sf_ga")):
        return BQ_NOT_GOOGLE.get(db, (BQ_GOOGLE, ""))[0]
    if re.fullmatch(r"sf\d+", instance_id):
        return SF_MARKET
    raise ValueError(instance_id)


# ---------------------------------------------------------------------------
# Structure

COL = re.compile(r'^\s*"?([^"\s]+)"?\s+([A-Za-z_]+(?:\([^)]*\))?)', re.M)


def columns(ddl: str) -> tuple:
    body = ddl[ddl.find("(") + 1: ddl.rfind(")")] if "(" in ddl else ""
    return tuple(sorted((n.lower(), t.upper()) for n, t in COL.findall(body)))


def load_structure(root: str) -> dict:
    """db -> {"tables": {(schema, TABLE): cols}}; empty DDLs dropped."""
    dbs: dict = collections.defaultdict(dict)
    empty = collections.Counter()
    for path in glob.glob(f"{root}/spider2-snow/resource/databases/*/*/DDL.csv"):
        db, schema = path.split(os.sep)[-3:-1]
        for row in csv.DictReader(open(path, encoding="utf-8")):
            cols = columns(row["DDL"] or "")
            if not cols:
                empty[db] += 1
                continue
            dbs[db][(schema.upper(), row["table_name"].upper())] = cols
    return dbs, empty


def mask(name: str) -> str:
    return re.sub(r"\d+", "#", name.upper()).rstrip("*").rstrip("_#") or "#"


# ---------------------------------------------------------------------------
# Tables read

DIALECT = {"bq": "bigquery", "ga": "bigquery", "local": "sqlite", "sf": "snowflake"}


def sql_tables(sql: str, dialect: str) -> list[tuple[str, str]]:
    """(schema, table) for every real table reference; CTE names removed."""
    out = []
    try:
        trees = sqlglot.parse(sql, read=dialect, error_level=sqlglot.ErrorLevel.IGNORE)
    except Exception:
        trees = []
    for tree in trees:
        if tree is None:
            continue
        ctes = {c.alias_or_name.upper() for c in tree.find_all(exp.CTE)}
        for t in tree.find_all(exp.Table):
            name = t.name
            if not name or (not t.db and name.upper() in ctes):
                continue
            out.append((t.db.upper(), name.upper()))
    return out


def resolve(ref: tuple[str, str], tables: dict) -> set[tuple[str, str]]:
    """Match one reference to the database's real tables (wildcards expand)."""
    schema, name = ref
    if name.endswith("*"):
        stem = name.rstrip("*")
        hit = {k for k in tables if k[1].startswith(stem) and (not schema or k[0] == schema)}
    else:
        hit = {k for k in tables if k[1] == name and (not schema or k[0] == schema)}
        if not hit:  # schema spelled differently across dialects
            hit = {k for k in tables if k[1] == name}
    return hit


def logical(keys: set[tuple[str, str]]) -> set[tuple[str, str]]:
    return {(s, mask(t)) for s, t in keys}


# ---------------------------------------------------------------------------


def share(xs, pred) -> float:
    xs = list(xs)
    return sum(1 for x in xs if pred(x)) / len(xs)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spider2", required=True)
    ap.add_argument("--out", default="spider2_shape.json")
    a = ap.parse_args()
    root = a.spider2
    rev = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True,
                         text=True).stdout.strip()

    snow = [json.loads(x) for x in open(f"{root}/spider2-snow/spider2-snow.jsonl")]
    lite = [json.loads(x) for x in open(f"{root}/spider2-lite/spider2-lite.jsonl")]
    db_of = {t["instance_id"]: t["db_id"] for t in snow}
    n = len(snow)
    R: dict = {"spider2_commit": rev, "tasks": n, "databases": len(set(db_of.values()))}

    # 1. Provenance -------------------------------------------------------
    prov = collections.Counter(provenance(t["instance_id"], t["db_id"]) for t in snow)
    R["provenance"] = {k: {"tasks": v, "share": v / n} for k, v in prov.most_common()}
    by_db = collections.Counter(db_of.values())
    syn = sum(by_db[d] for d in SYNTHETIC)
    part = sum(by_db[d] for d in PARTLY_SYNTHETIC)
    R["synthetic"] = {
        "databases": len(SYNTHETIC), "tasks": syn, "share": syn / n,
        "partly_databases": len(PARTLY_SYNTHETIC), "partly_tasks": part,
        "upper_share": (syn + part) / n,
        "rows": sorted(([d, by_db[d], why] for d, why in SYNTHETIC.items()),
                       key=lambda r: -r[1]),
    }
    # The original 632-task release (commit 9615ebf) splits by host engine.
    try:
        full = subprocess.run(
            ["git", "-C", root, "show",
             "9615ebf:spider2/evaluation_suite/gold/spider2_eval.jsonl"],
            capture_output=True, text=True, check=True).stdout.splitlines()
        pre = collections.Counter(re.sub(r"\d+$", "", json.loads(x)["instance_id"])
                                  for x in full)
        R["release_632"] = {
            "tasks": len(full),
            "bigquery": pre["bq"] + pre["ga"],
            "snowflake": pre["sf_bq"] + pre["sf"],
            "snowflake_bigquery_rehosted": pre["sf_bq"],
            "snowflake_marketplace": pre["sf"],
            "local": pre["local"],
        }
    except subprocess.CalledProcessError:
        pass

    # 2. Structure --------------------------------------------------------
    dbs, empty = load_structure(root)
    fam_rows = []
    total = fams = 0
    for db, tables in dbs.items():
        groups = collections.Counter(tables.values())
        total += len(tables)
        fams += len(groups)
        fam_rows.append({"db": db, "tables": len(tables), "families": len(groups),
                         "largest_family": max(groups.values()), "tasks": by_db[db]})
    fam_rows.sort(key=lambda r: -(r["tables"] - r["families"]))
    top = fam_rows[0]
    rest_t, rest_f = total - top["tables"], fams - top["families"]
    fam_by_task = [r["families"] for r in fam_rows for _ in range(r["tasks"])]
    R["structure"] = {
        "tables": total, "families": fams, "copy_share": 1 - fams / total,
        "empty_ddl_skipped": sum(empty.values()),
        "empty_ddl_databases": sorted(empty),
        "largest_copier": top["db"], "copy_share_without_largest": 1 - rest_f / rest_t,
        "median_families_per_db": statistics.median(r["families"] for r in fam_rows),
        "quartiles_families_per_db": statistics.quantiles(
            [r["families"] for r in fam_rows], n=4),
        "max_families": max(fam_rows, key=lambda r: r["families"]),
        "median_families_per_task": statistics.median(fam_by_task),
        "tasks_on_db_with_family_ge_50": sum(r["tasks"] for r in fam_rows
                                             if r["largest_family"] >= 50),
        "tasks_on_db_with_family_ge_10": sum(r["tasks"] for r in fam_rows
                                             if r["largest_family"] >= 10),
        "tasks_on_db_with_le_5_families": sum(r["tasks"] for r in fam_rows
                                              if r["families"] <= 5),
        "per_db": fam_rows,
    }

    # 3. Tables read ------------------------------------------------------
    # 3a. The authors' gold-tables lists (all tasks).
    gt, gt_raw = {}, {}
    unresolved_gt = 0
    for x in open(f"{root}/methods/gold-tables/spider2-snow-gold-tables.jsonl"):
        r = json.loads(x)
        keys = set()
        for full_name in r["gold_tables"]:
            parts = full_name.upper().split(".")
            keys.add((parts[-2] if len(parts) > 1 else "", parts[-1]))
        gt[r["instance_id"]] = len(logical(keys))
        gt_raw[r["instance_id"]] = sorted(r["gold_tables"])
        tables = dbs.get(db_of[r["instance_id"]], {})
        unresolved_gt += sum(1 for k in keys if not resolve(k, tables))
    # 3b. Gold SQL, both suites. Lite ids map to snow ids by prefix; both copies
    # are checked against the same question text.
    snow_q = {t["instance_id"]: t["instruction"].strip() for t in snow}
    lite_q = {t["instance_id"]: t["question"].strip() for t in lite}

    def snow_id(lite_id: str) -> str:
        return lite_id if lite_id.startswith("sf") else "sf_" + lite_id

    parsed: dict = collections.defaultdict(dict)  # snow id -> suite -> logical tables
    dropped = kept_unmatched = 0
    for suite in ("lite", "snow"):
        for path in sorted(glob.glob(f"{root}/spider2-{suite}/evaluation_suite/gold/sql/*.sql")):
            iid = os.path.basename(path)[:-4]
            sid = iid if suite == "snow" else snow_id(iid)
            if sid not in db_of or (suite == "lite" and lite_q.get(iid) != snow_q.get(sid)):
                continue
            prefix = re.match(r"(sf_bq|sf_ga|sf_local|bq|ga|local|sf)", iid).group(1)
            dialect = "snowflake" if suite == "snow" or prefix.startswith("sf") \
                else DIALECT[prefix]
            tables = dbs.get(db_of[sid], {})
            hits = set()
            for ref in sql_tables(open(path, encoding="utf-8").read(), dialect):
                m = resolve(ref, tables)
                if m:
                    hits |= m
                elif ref[0]:
                    kept_unmatched += 1
                    hits.add(ref)
                else:
                    dropped += 1
            parsed[sid][suite] = len(logical(hits))
    sql = {sid: max(v.values()) for sid, v in parsed.items()}
    both = {sid: v for sid, v in parsed.items() if len(v) == 2}

    def dist(counts):
        c = collections.Counter(counts)
        return {str(k): c[k] for k in sorted(c)}

    agree = [sid for sid in sql if sql[sid] == gt.get(sid)]
    R["tables_read"] = {
        "gold_sql": {
            "tasks": len(sql), "share_of_tasks": len(sql) / n,
            "databases": len({db_of[s] for s in sql}),
            "distribution": dist(sql.values()),
            "one": share(sql.values(), lambda k: k == 1),
            "le_two": share(sql.values(), lambda k: k <= 2),
            "ge_five": sum(1 for k in sql.values() if k >= 5),
            "max": max(sql.values()),
            "median": statistics.median(sql.values()),
            "mean": statistics.mean(sql.values()),
            "unmatched_unqualified_dropped": dropped,
            "unmatched_qualified_kept": kept_unmatched,
            "in_both_suites": len(both),
            "suites_disagree": sum(1 for v in both.values() if v["lite"] != v["snow"]),
            "agrees_with_gold_tables": len(agree) / len(sql),
            "agrees_with_gold_tables_n": len(agree),
            "gold_tables_le_two_on_same_tasks":
                share((gt[s] for s in sql), lambda k: k <= 2),
        },
        "gold_tables": {
            "tasks": len(gt), "distribution": dist(gt.values()),
            "one": share(gt.values(), lambda k: k == 1),
            "le_two": share(gt.values(), lambda k: k <= 2),
            "ge_five": sum(1 for k in gt.values() if k >= 5),
            "max": max(gt.values()),
            "median": statistics.median(gt.values()),
            "mean": statistics.mean(gt.values()),
            "unresolved_references": unresolved_gt,
            "raw_tables_median": None,
        },
        "per_task": {s: {"db": db_of[s], "gold_sql": sql.get(s),
                         "gold_sql_by_suite": parsed.get(s), "gold_tables": gt[s],
                         "gold_tables_raw": gt_raw[s]}
                     for s in sorted(gt)},
    }
    raw = [len(json.loads(x)["gold_tables"]) for x in
           open(f"{root}/methods/gold-tables/spider2-snow-gold-tables.jsonl")]
    R["tables_read"]["gold_tables"]["raw_tables_median"] = statistics.median(raw)
    R["tables_read"]["gold_tables"]["raw_tables_max"] = max(raw)

    json.dump(R, open(a.out, "w"), indent=1)
    s, g, st = R["tables_read"]["gold_sql"], R["tables_read"]["gold_tables"], R["structure"]
    print(f"Spider2 @ {rev[:8]}: {n} tasks, {R['databases']} databases")
    for k, v in R["provenance"].items():
        print(f"  {v['tasks']:4d} {100 * v['share']:5.1f}%  {k}")
    print(f"  synthetic/sample: {syn} tasks ({100 * syn / n:.1f}%), "
          f"<= {100 * R['synthetic']['upper_share']:.1f}% with partly synthetic")
    if "release_632" in R:
        print("  632 release:", R["release_632"])
    print(f"structure: {st['tables']} tables, {st['families']} families, "
          f"copies {100 * st['copy_share']:.1f}% "
          f"({100 * st['copy_share_without_largest']:.1f}% without {st['largest_copier']}); "
          f"skipped {st['empty_ddl_skipped']} empty DDLs in {st['empty_ddl_databases']}")
    print(f"gold SQL: {s['tasks']} tasks ({100 * s['share_of_tasks']:.1f}%), "
          f"{s['databases']} dbs, dist {s['distribution']}, one {100 * s['one']:.1f}%, "
          f"<=2 {100 * s['le_two']:.1f}%, max {s['max']}, agree w/ gold-tables "
          f"{100 * s['agrees_with_gold_tables']:.1f}%; dropped {s['unmatched_unqualified_dropped']}"
          f", kept {s['unmatched_qualified_kept']}; suites disagree "
          f"{s['suites_disagree']}/{s['in_both_suites']}")
    print(f"gold tables: {g['tasks']} tasks, dist {g['distribution']}, one {100 * g['one']:.1f}%,"
          f" <=2 {100 * g['le_two']:.1f}%, >=5 {g['ge_five']}, max {g['max']}, "
          f"unresolved {g['unresolved_references']}")


if __name__ == "__main__":
    main()
