"""Reference solution for ds-33-orders-by-rating-shown.

The question asks for each month of July-December 2024 how many orders were placed at
storefronts showing each star-rating band at the moment of ordering. The app's own record of
what it showed is the nightly rating history, ``XX_MERCHANT_RATING_SNAPSHOTS`` (one row per
storefront per ``AS_OF_DATE``, ``DISPLAYED_RATING`` null until the storefront has enough
reviews). The trap is which row a customer saw. The prompt says ratings are recalculated at
midnight from the reviews left up to then; the rows show that a row dated D already counts
the reviews posted on D (its ``RATING_COUNT`` moves by exactly that day's reviews), so it is
the midnight recalculation that closes D and is what the app shows all day on D+1. An order
placed on day d therefore saw the row dated d - 1; joining the row of the order's own date
reads a rating that did not exist yet, and moves orders between bands (most visibly out of
``not rated``). The storefront's current rating and a recount of the reviews are wrong too.

Orders (``OE_ORDER_HEADERS_ALL``, every status, by ``ORDERED_DATE``, New York local already)
name their storefront by ``SHIP_FROM_ORG_ID``; ``HR_ORGANIZATION_INFORMATION`` rows of context
``XX_STOREFRONT`` carry that storefront's supplier id (``ORG_INFORMATION1``), which is the
snapshots' ``VENDOR_ID``. No row the night before (the storefront's first day) or a null
rating is ``not rated``; the bands use the recorded rating unrounded.

Score: 1.0 on the paper's warehouse and 1.0 on the released warehouse.
"""

QUESTION = "ds-33-orders-by-rating-shown"

_DIALECT = {
    "bigquery": {"day_before": "DATE_SUB({d}, INTERVAL 1 DAY)",
                 "day_after": "DATE_ADD({d}, INTERVAL 1 DAY)",
                 "month": "FORMAT_DATE('%Y-%m', {d})", "to_int": "SAFE_CAST({x} AS INT64)"},
    "duckdb": {"day_before": "CAST({d} - INTERVAL 1 DAY AS DATE)",
               "day_after": "CAST({d} + INTERVAL 1 DAY AS DATE)",
               "month": "strftime({d}, '%Y-%m')", "to_int": "TRY_CAST({x} AS BIGINT)"},
}

# 1. Which reviews does a row dated D hold? Among storefronts still under the 100-review
#    window, compare each row's change in RATING_COUNT from the row the day before with the
#    reviews posted on D, on the day before, and on the day after (June 2024 is enough).
_REVIEWS_IN_ROW = """\
WITH snap AS (
  SELECT VENDOR_ID, AS_OF_DATE, RATING_COUNT,
         LAG(RATING_COUNT) OVER (PARTITION BY VENDOR_ID ORDER BY AS_OF_DATE) AS prev_count,
         LAG(AS_OF_DATE) OVER (PARTITION BY VENDOR_ID ORDER BY AS_OF_DATE) AS prev_date
  FROM XX_MERCHANT_RATING_SNAPSHOTS
  WHERE AS_OF_DATE >= '2024-05-31' AND AS_OF_DATE < '2024-07-01'
), rev AS (
  SELECT VENDOR_ID, CAST(REVIEW_DATE AS DATE) AS posted_on, COUNT(*) AS reviews
  FROM XX_REVIEWS
  WHERE REVIEW_DATE >= '2024-05-30' AND REVIEW_DATE < '2024-07-02'
  GROUP BY VENDOR_ID, posted_on
)
SELECT COUNT(*) AS rows_checked,
       SUM(CASE WHEN s.RATING_COUNT - s.prev_count = COALESCE(r0.reviews, 0) THEN 1 ELSE 0 END)
         AS change_equals_reviews_on_row_date,
       SUM(CASE WHEN s.RATING_COUNT - s.prev_count = COALESCE(r1.reviews, 0) THEN 1 ELSE 0 END)
         AS change_equals_reviews_day_before,
       SUM(CASE WHEN s.RATING_COUNT - s.prev_count = COALESCE(r2.reviews, 0) THEN 1 ELSE 0 END)
         AS change_equals_reviews_day_after
FROM snap s
LEFT JOIN rev r0 ON r0.VENDOR_ID = s.VENDOR_ID AND r0.posted_on = s.AS_OF_DATE
LEFT JOIN rev r1 ON r1.VENDOR_ID = s.VENDOR_ID AND r1.posted_on = {before_row}
LEFT JOIN rev r2 ON r2.VENDOR_ID = s.VENDOR_ID AND r2.posted_on = {after_row}
WHERE s.AS_OF_DATE >= '2024-06-01' AND s.prev_date = {before_row}
  AND s.prev_count < 100 AND s.RATING_COUNT < 100
  AND COALESCE(r0.reviews, 0) + COALESCE(r1.reviews, 0) + COALESCE(r2.reviews, 0) > 0"""

# 2. Orders per storefront and day, each with the row dated the night before.
_BANDS = """\
WITH daily_orders AS (
  SELECT CAST(ORDERED_DATE AS DATE) AS order_day, SHIP_FROM_ORG_ID, COUNT(*) AS orders
  FROM OE_ORDER_HEADERS_ALL
  WHERE ORDERED_DATE >= '2024-07-01' AND ORDERED_DATE < '2025-01-01'
  GROUP BY order_day, SHIP_FROM_ORG_ID
), shown AS (
  SELECT d.order_day, d.orders, i.ORGANIZATION_ID AS storefront_org, s.DISPLAYED_RATING
  FROM daily_orders d
  LEFT JOIN HR_ORGANIZATION_INFORMATION i
    ON i.ORGANIZATION_ID = d.SHIP_FROM_ORG_ID AND i.ORG_INFORMATION_CONTEXT = 'XX_STOREFRONT'
  LEFT JOIN XX_MERCHANT_RATING_SNAPSHOTS s
    ON s.VENDOR_ID = {vendor_id} AND s.AS_OF_DATE = {night_before}
)
SELECT {month} AS month,
       CASE WHEN DISPLAYED_RATING >= 4.5 THEN '4.5 and up'
            WHEN DISPLAYED_RATING >= 4.0 THEN '4.0 to 4.5'
            WHEN DISPLAYED_RATING < 4.0 THEN 'under 4.0'
            ELSE 'not rated' END AS rating_band,
       SUM(orders) AS orders,
       SUM(CASE WHEN storefront_org IS NULL THEN orders ELSE 0 END) AS orders_without_storefront
FROM shown
GROUP BY month, rating_band
ORDER BY month, rating_band"""


def _sql(template: str) -> dict:
    out = {}
    for engine, d in _DIALECT.items():
        out[engine] = template.format(
            before_row=d["day_before"].format(d="s.AS_OF_DATE"),
            after_row=d["day_after"].format(d="s.AS_OF_DATE"),
            night_before=d["day_before"].format(d="d.order_day"),
            vendor_id=d["to_int"].format(x="i.ORG_INFORMATION1"),
            month=d["month"].format(d="order_day"))
    return out


STEPS = [
    # 1. Does a row dated D already hold D's reviews? If so it is first shown on D + 1.
    ("run_sql", {"sql": _sql(_REVIEWS_IN_ROW)}),
    # 2. Band each storefront-day's orders on the row dated the night before.
    ("run_sql", {"sql": _sql(_BANDS)}),

    # 3. Every order maps to a storefront; publish the 24 month-band counts.
    ("run_python", {"code": """\
import pandas as pd
from mission_control import MissionControl

print(pd.read_parquet("results/sql_0001.parquet").T.to_string(header=False), "\\n")

df = pd.read_parquet("results/sql_0002.parquet")
assert df["orders_without_storefront"].sum() == 0
df = (df[["month", "rating_band", "orders"]].astype({"orders": int})
      .sort_values(["month", "rating_band"]).reset_index(drop=True))
print(df.pivot(index="month", columns="rating_band", values="orders").to_string())

mission_control = MissionControl()
mission_control.publish_data_sources([
    {"name": "orders_by_rating_shown_monthly_2024h2", "frame": df},
])
mission_control.summary()
"""}),
]
