"""Reference solution for ds-22-margin-monthly-per-order.

The question gives Finance's definition of contribution margin and asks for the monthly
margin per order for 2024, "from our platform's order-level P&L as booked". What it leaves
out is where that P&L lives, and how the two terms booked at a coarser grain (the
membership fee, the minimum-pay true-up) land on orders. The books already carry every term
per order: ``GL_IMPORT_REFERENCES`` ties each journal line to its order (``REFERENCE_2`` is
the order number, ``REFERENCE_4`` the line type), including the membership fee credited to
the order and the minimum-pay accrual charged to the delivery.

Ten line types are Finance's list, one to one: kept, ``PLATFORM_REVENUE`` (commission and
fees, net of courier base pay) and ``MEMBERSHIP_FEE_ALLOCATED`` (the member's fee credited to
the orders it funded); borne, ``PROCESSOR_FEES``, ``PROMO_EXPENSE``, ``REFUND_EXPENSE``,
``CHARGEBACK_EXPENSE``, ``CANCELLATION_EXPENSE``, ``REFERRAL_EXPENSE``, ``QUEST_EXPENSE`` and
``MIN_PAY_EXPENSE`` (the true-up accrued on the delivery). A line's amount is on the
subledger line it came from (``XLA_AE_LINES``, by ``GL_SL_LINK_ID``) or on the custom feed's
interface row (``XX_GL_INTERFACE_HIST``, by ``REFERENCE_7``); credits minus debits is margin.
The weekly true-up actually paid, and its reversal of the accrual, carry no order number and
drop out at the join to the orders. The denominator is every order placed in the month,
every status.

Steps 1, 3 and 4 read the whole year's books, and on BigQuery each scans more than the
release's 20 GiB per-query cap: replay this one on DuckDB.

Score: 1.0 on the paper's warehouse (12 of 12 months) and 1.0 on the released warehouse.
"""

QUESTION = "ds-22-margin-monthly-per-order"

LINE_TYPES = ("'PLATFORM_REVENUE', 'MEMBERSHIP_FEE_ALLOCATED', 'PROCESSOR_FEES', "
              "'PROMO_EXPENSE', 'REFUND_EXPENSE', 'CHARGEBACK_EXPENSE', "
              "'CANCELLATION_EXPENSE', 'REFERRAL_EXPENSE', 'QUEST_EXPENSE', 'MIN_PAY_EXPENSE'")

_MARGIN_BY_SOURCE = """\
SELECT FORMAT_DATETIME('%Y-%m', o.ORDERED_DATE) AS month,
       r.REFERENCE_4 AS line_type,
       SUM(COALESCE(CAST(a.{cr} AS NUMERIC), 0) - COALESCE(CAST(a.{dr} AS NUMERIC), 0))
         AS margin_usd
FROM GL_IMPORT_REFERENCES r
JOIN {source} a ON a.{source_key} = {ref_key}
JOIN OE_ORDER_HEADERS_ALL o ON o.ORDER_NUMBER = SAFE_CAST(r.REFERENCE_2 AS INT64)
WHERE r.REFERENCE_4 IN ({line_types})
  AND o.ORDERED_DATE >= '2024-01-01' AND o.ORDERED_DATE < '2025-01-01'
GROUP BY month, line_type"""

#: The same statement in DuckDB's spelling.
_MARGIN_BY_SOURCE_DUCKDB = """\
SELECT strftime(o.ORDERED_DATE, '%Y-%m') AS month,
       r.REFERENCE_4 AS line_type,
       SUM(COALESCE(CAST(a.{cr} AS DECIMAL(38, 9)), 0)
           - COALESCE(CAST(a.{dr} AS DECIMAL(38, 9)), 0)) AS margin_usd
FROM GL_IMPORT_REFERENCES r
JOIN {source} a ON a.{source_key} = {ref_key}
JOIN OE_ORDER_HEADERS_ALL o ON o.ORDER_NUMBER = TRY_CAST(r.REFERENCE_2 AS BIGINT)
WHERE r.REFERENCE_4 IN ({line_types})
  AND o.ORDERED_DATE >= '2024-01-01' AND o.ORDERED_DATE < '2025-01-01'
GROUP BY month, line_type"""

STEPS = [
    # 1. What kinds of journal lines do the books tie to an order? REFERENCE_4 names them.
    ("run_sql", {"sql": {"bigquery": """\
SELECT REFERENCE_4 AS line_type, COUNT(*) AS lines,
       COUNTIF(SAFE_CAST(REFERENCE_2 AS INT64) IS NOT NULL) AS with_order_number
FROM GL_IMPORT_REFERENCES
GROUP BY line_type
ORDER BY lines DESC""", "duckdb": """\
SELECT REFERENCE_4 AS line_type, COUNT(*) AS lines,
       COUNT_IF(TRY_CAST(REFERENCE_2 AS BIGINT) IS NOT NULL) AS with_order_number
FROM GL_IMPORT_REFERENCES
GROUP BY line_type
ORDER BY lines DESC"""}}),
    # Finance's list maps one-to-one onto ten of them:
    #   kept   PLATFORM_REVENUE (commission and fees, net of courier base pay),
    #          MEMBERSHIP_FEE_ALLOCATED (the member's fee credited to the orders it funded)
    #   borne  PROCESSOR_FEES, PROMO_EXPENSE, REFUND_EXPENSE, CHARGEBACK_EXPENSE,
    #          CANCELLATION_EXPENSE, REFERRAL_EXPENSE, QUEST_EXPENSE,
    #          MIN_PAY_EXPENSE (the true-up accrued on the delivery)

    # 2. The denominator: every order placed in each month, every status.
    ("run_sql", {"sql": {"bigquery": """\
SELECT FORMAT_DATETIME('%Y-%m', ORDERED_DATE) AS month, COUNT(*) AS orders
FROM OE_ORDER_HEADERS_ALL
WHERE ORDERED_DATE >= '2024-01-01' AND ORDERED_DATE < '2025-01-01'
GROUP BY month""", "duckdb": """\
SELECT strftime(ORDERED_DATE, '%Y-%m') AS month, COUNT(*) AS orders
FROM OE_ORDER_HEADERS_ALL
WHERE ORDERED_DATE >= '2024-01-01' AND ORDERED_DATE < '2025-01-01'
GROUP BY month"""}}),

    # 3-4. The numerator. A line's amount is on the subledger line it came from
    # (XLA_AE_LINES, via GL_SL_LINK_ID) or on the custom feed's interface row
    # (XX_GL_INTERFACE_HIST, via REFERENCE_7): one query per source. Credits minus debits
    # is margin. The weekly true-up actually paid, and its reversal of the accrual, carry no
    # order number and drop out at the join to the orders.
    ("run_sql", {"sql": {
        "bigquery": _MARGIN_BY_SOURCE.format(
            source="XLA_AE_LINES", source_key="GL_SL_LINK_ID", ref_key="r.GL_SL_LINK_ID",
            cr="ACCOUNTED_CR", dr="ACCOUNTED_DR", line_types=LINE_TYPES),
        "duckdb": _MARGIN_BY_SOURCE_DUCKDB.format(
            source="XLA_AE_LINES", source_key="GL_SL_LINK_ID", ref_key="r.GL_SL_LINK_ID",
            cr="ACCOUNTED_CR", dr="ACCOUNTED_DR", line_types=LINE_TYPES)}}),
    ("run_sql", {"sql": {
        "bigquery": _MARGIN_BY_SOURCE.format(
            source="XX_GL_INTERFACE_HIST", source_key="INTERFACE_LINE_ID",
            ref_key="SAFE_CAST(r.REFERENCE_7 AS INT64)",
            cr="ENTERED_CR", dr="ENTERED_DR", line_types=LINE_TYPES),
        "duckdb": _MARGIN_BY_SOURCE_DUCKDB.format(
            source="XX_GL_INTERFACE_HIST", source_key="INTERFACE_LINE_ID",
            ref_key="TRY_CAST(r.REFERENCE_7 AS BIGINT)",
            cr="ENTERED_CR", dr="ENTERED_DR", line_types=LINE_TYPES)}}),

    # 5. Load the results, divide, publish.
    ("run_python", {"code": """\
import pandas as pd
from mission_control import MissionControl

orders = pd.read_parquet("results/sql_0002.parquet")
lines = pd.concat([pd.read_parquet("results/sql_0003.parquet"),
                   pd.read_parquet("results/sql_0004.parquet")])
lines["margin_usd"] = lines["margin_usd"].astype(float)

pnl = lines.pivot_table(index="month", columns="line_type", values="margin_usd",
                        aggfunc="sum", fill_value=0.0)
print((pnl / 1e6).round(3).to_string(), "\\n($ millions)\\n")

df = orders.merge(pnl.sum(axis=1).rename("margin_usd").reset_index(), on="month")
df["contribution_margin_per_order_usd"] = (df["margin_usd"] / df["orders"]).round(4)
df = (df[["month", "orders", "contribution_margin_per_order_usd"]]
      .sort_values("month").reset_index(drop=True))
print(df.to_string(index=False))

mission_control = MissionControl()
mission_control.publish_data_sources([
    {"name": "contribution_margin_monthly_per_order_2024", "frame": df},
])
mission_control.note(
    "Contribution margin = credits minus debits on every journal line the books tie to an "
    "order (GL_IMPORT_REFERENCES.REFERENCE_2 = ORDER_NUMBER) for the ten line types in "
    "Finance's definition, amounts from XLA_AE_LINES or XX_GL_INTERFACE_HIST; orders by "
    "ORDERED_DATE, every status. The minimum-pay term is the per-delivery accrual; the weekly "
    "payment and its reversal carry no order and are not per-order costs.")
mission_control.summary()
"""}),
]
