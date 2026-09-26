"""The system prompt and the dataset a question reads, from its as-of month.

A question carries a data cutoff (``2024-09``): it reads ``<base>_9``, a schema of views that
stops at the end of September, and the system prompt says how far the warehouse runs.
December is the full year, which is the base schema itself.
"""

from __future__ import annotations

import calendar
from pathlib import Path

TEMPLATE = (Path(__file__).resolve().parent / "system_prompt.md").read_text(encoding="utf-8")
DIALECTS = {"duckdb": "DuckDB SQL", "bigquery": "BigQuery GoogleSQL"}


def month_index(month: str) -> int:
    """``"2024-09"`` -> 9; ``""`` (the full year) -> 12."""
    return int(month.split("-")[1]) if month else 12


def dataset_for(base: str, month: str) -> str:
    """``food_delivery``, ``2024-09`` -> ``food_delivery_9``; December is the base."""
    index = month_index(month)
    return base if index == 12 else f"{base}_{index}"


def coverage(month: str) -> str:
    if month_index(month) == 12:
        return "the calendar year 2024"
    year, index = month.split("-")
    return (f"the calendar year {year} to date, current as of the end of "
            f"{calendar.month_name[int(index)]} {year}")


def system_prompt(engine: str, month: str) -> str:
    return (TEMPLATE.replace("{dialect}", DIALECTS.get(engine, engine))
            .replace("{coverage}", coverage(month)))
