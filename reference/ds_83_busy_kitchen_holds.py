"""Reference solution for ds-83-busy-kitchen-holds.

The question asks, for each month of 2024 by the day the order was placed, how many orders
Busy Kitchen held, the minutes the holds added to their quoted prep times, and how many
storefronts held at least one order. The warehouse keeps no hold flag. Each order's prep
quote (``XX_ORDER_PREP_QUOTES``) says who set it: ``MERCHANT_ORDER`` is a quote the storefront
set on this order rather than its standing default, but most of those are the large-order
bump the prompt warns about, not holds. Counting every ``MERCHANT_ORDER`` quote, or every one at
a Busy Kitchen storefront, overstates the tile many times over; counting only quotes off the
5-minute grid drops holds of whole multiples of five.

The bump rule can be read off the storefronts that set their own prep times without Busy
Kitchen (``XX_MERCHANT_INTEGRATIONS``: ``PREP_TIME_SOURCE_CODE = 'MERCHANT_SET'``,
``BUSY_KITCHEN_FLAG = 'N'``): their ``MERCHANT_ORDER`` quotes are the storefront's
``DEFAULT_PREP_MINUTES`` plus exactly 5 minutes for 6 to 11 items and 10 minutes for 12 or
more, items being the ``ORDERED_QUANTITY`` summed over the order's ``STANDARD`` lines
(``SERVICE`` lines are fees). The hold is what is left of a ``MERCHANT_ORDER`` quote after the
default and that bump; an order with a positive remainder was held. The remainder decides,
not ``BUSY_KITCHEN_FLAG``, which is today's setting (a storefront may have switched Busy
Kitchen off since); all but a handful of held orders are at storefronts with it on today.
Orders reach their storefront's settings through ``SHIP_FROM_ORG_ID`` →
``HR_ORGANIZATION_INFORMATION`` (``XX_STOREFRONT``, ``ORG_INFORMATION1`` = ``VENDOR_ID``).
Every status counts. The year is read a quarter at a time to keep each BigQuery scan small
(the tables are partitioned by creation date; lines are created with the order, quotes within
the hour after it).

Score: 1.0 on the paper's warehouse and 1.0 on the released warehouse.
"""

QUESTION = "ds-83-busy-kitchen-holds"

_INT = {"bigquery": "SAFE_CAST(h.ORG_INFORMATION1 AS INT64)",
        "duckdb": "TRY_CAST(h.ORG_INFORMATION1 AS BIGINT)"}

# Each storefront-set quote with its storefront's settings, the order's item count and the
# minutes left after the default and the large-order bump.
_QUOTES = """\
WITH quotes AS (
  SELECT HEADER_ID, QUOTED_PREP_MINUTES
  FROM XX_ORDER_PREP_QUOTES
  WHERE QUOTE_SOURCE_CODE = 'MERCHANT_ORDER'
    AND CREATION_DATE >= '{lo}' AND CREATION_DATE < '{quote_hi}'
), items AS (
  SELECT HEADER_ID, SUM(ORDERED_QUANTITY) AS items
  FROM OE_ORDER_LINES_ALL
  WHERE ITEM_TYPE_CODE = 'STANDARD' AND CREATION_DATE >= '{lo}' AND CREATION_DATE < '{hi}'
  GROUP BY HEADER_ID
), quoted AS (
  SELECT o.ORDERED_DATE, i.VENDOR_ID, i.BUSY_KITCHEN_FLAG, i.PREP_TIME_SOURCE_CODE,
         COALESCE(n.items, 0) AS items,
         q.QUOTED_PREP_MINUTES - i.DEFAULT_PREP_MINUTES AS over_default,
         CASE WHEN COALESCE(n.items, 0) >= 12 THEN 10
              WHEN COALESCE(n.items, 0) >= 6 THEN 5 ELSE 0 END AS bump
  FROM quotes q
  JOIN OE_ORDER_HEADERS_ALL o ON o.HEADER_ID = q.HEADER_ID
  JOIN HR_ORGANIZATION_INFORMATION h
    ON h.ORGANIZATION_ID = o.SHIP_FROM_ORG_ID AND h.ORG_INFORMATION_CONTEXT = 'XX_STOREFRONT'
  JOIN XX_MERCHANT_INTEGRATIONS i ON i.VENDOR_ID = {vendor_id}
  LEFT JOIN items n ON n.HEADER_ID = q.HEADER_ID
  WHERE o.CREATION_DATE >= '{lo}' AND o.CREATION_DATE < '{hi}'
    AND o.ORDERED_DATE >= '{lo}' AND o.ORDERED_DATE < '{hi}'
)
"""

# 1. How far above its default does a storefront set a quote, by the order's size, at
#    storefronts with and without Busy Kitchen? (March 2024 is enough to see the rule.)
_BUMP_RULE = _QUOTES + """\
SELECT BUSY_KITCHEN_FLAG, PREP_TIME_SOURCE_CODE,
       CASE WHEN items >= 12 THEN '12 or more' WHEN items >= 6 THEN '6 to 11'
            ELSE 'under 6' END AS items_band,
       COUNT(*) AS quotes,
       MIN(over_default) AS min_over_default, MAX(over_default) AS max_over_default,
       SUM(CASE WHEN over_default = bump THEN 1 ELSE 0 END) AS exactly_default_plus_bump,
       SUM(CASE WHEN over_default > bump THEN 1 ELSE 0 END) AS above_default_plus_bump
FROM quoted
GROUP BY BUSY_KITCHEN_FLAG, PREP_TIME_SOURCE_CODE, items_band
ORDER BY BUSY_KITCHEN_FLAG, PREP_TIME_SOURCE_CODE, items_band"""

# 2-5. The holds: what is left of the quote after the default and the bump, per month placed.
_HOLDS = _QUOTES + """\
SELECT EXTRACT(MONTH FROM ORDERED_DATE) AS month_num,
       COUNT(*) AS orders_held,
       SUM(over_default - bump) AS hold_minutes,
       COUNT(DISTINCT VENDOR_ID) AS storefronts,
       SUM(CASE WHEN BUSY_KITCHEN_FLAG = 'Y' THEN 1 ELSE 0 END) AS at_busy_kitchen_storefronts
FROM quoted
WHERE over_default - bump > 0
GROUP BY month_num"""

#: Orders by quarter placed; a quote is written within the hour after its order, so its
#: window runs a day past the quarter's end.
QUARTERS = [("2024-01-01", "2024-04-01", "2024-04-02"), ("2024-04-01", "2024-07-01", "2024-07-02"),
            ("2024-07-01", "2024-10-01", "2024-10-02"), ("2024-10-01", "2025-01-01", "2025-01-02")]


def _sql(template: str, lo: str, hi: str, quote_hi: str) -> dict:
    return {engine: template.format(lo=lo, hi=hi, quote_hi=quote_hi, vendor_id=v)
            for engine, v in _INT.items()}


STEPS = [
    # 1. Read the large-order bump off the storefronts without Busy Kitchen, and see that
    #    only Busy Kitchen storefronts quote above it.
    ("run_sql", {"sql": _sql(_BUMP_RULE, "2024-03-01", "2024-04-01", "2024-04-02")}),
    # 2-5. Held orders, hold minutes and storefronts per month, a quarter at a time.
    *[("run_sql", {"sql": _sql(_HOLDS, lo, hi, qhi)}) for lo, hi, qhi in QUARTERS],

    # 6. Stack the quarters and publish.
    ("run_python", {"code": """\
import pandas as pd
from mission_control import MissionControl

print(pd.read_parquet("results/sql_0001.parquet").to_string(index=False), "\\n")

held = pd.concat([pd.read_parquet(f"results/sql_{n:04d}.parquet") for n in range(2, 6)])
held = held.sort_values("month_num").reset_index(drop=True)
print("held orders at storefronts with Busy Kitchen on today:",
      int(held["at_busy_kitchen_storefronts"].sum()), "of", int(held["orders_held"].sum()), "\\n")
df = pd.DataFrame({
    "month": [f"2024-{int(m):02d}" for m in held["month_num"]],
    "orders_held": held["orders_held"].astype(int),
    "hold_minutes": held["hold_minutes"].astype(float).round(1),
    "storefronts": held["storefronts"].astype(int),
})
print(df.to_string(index=False))

mission_control = MissionControl()
mission_control.publish_data_sources([{"name": "busy_kitchen_holds_monthly_2024", "frame": df}])
mission_control.summary()
"""}),
]
