"""Reference solution for mer-14-platform-funded-at-shells.

Finance wants three figures for each storefront trust & safety confirmed as selling to its own
accounts (the prompt lists their payables VENDOR_IDs): the platform's share of the promotional
discounts on its pickup orders, the compensation we paid it for customer cancellations whose
charge was waived, and its five-star reviews. The traps are in where each figure lives. A
discount's funding split is on the order's price adjustment (``OE_PRICE_ADJUSTMENTS``:
``ATTRIBUTE1`` the platform's share, ``ATTRIBUTE2`` the storefront's; the line-level member
benefits on delivery fees carry no split and are not promotions). Who cancelled an order is on the header
(``ATTRIBUTE5``), not on the support case, and the support case's override amount is what the
customer was quoted, not what the storefront was paid: the compensation is the
``CANCELLATION_EXPENSE`` journal line the books tie to the order (``GL_IMPORT_REFERENCES``,
``REFERENCE_2`` the order number, amount on the custom feed's ``XX_GL_INTERFACE_HIST`` row by
``REFERENCE_7``). The platform also compensates the storefronts for the orders it cancelled
itself; those are not customer cancellations and stay out.

The rule filed: storefronts reached through ``HR_ORGANIZATION_INFORMATION`` (``XX_STOREFRONT``,
the VENDOR_ID in ``ORG_INFORMATION1``) and their 2024 orders by ``SHIP_FROM_ORG_ID``; the
platform share summed over pickup orders that went through (not cancelled or rejected); the
cancellation expense on orders the customer cancelled, a quarter of the books at a time; and
the five-star reviews (``XX_REVIEWS``) of the storefront's 2024 orders. Every query is
restricted to the listed storefronts.

Score: 1.0 on the paper's warehouse and 1.0 on the released warehouse.
"""

import re

QUESTION = "mer-14-platform-funded-at-shells"

#: The listed storefronts and their 2024 orders.
_ORDERS = """\
WITH storefront AS (
  SELECT ORGANIZATION_ID, ORG_INFORMATION1 AS vendor_id
  FROM HR_ORGANIZATION_INFORMATION
  WHERE ORG_INFORMATION_CONTEXT = 'XX_STOREFRONT' AND ORG_INFORMATION1 IN ({ids})
),
orders AS (
  SELECT h.HEADER_ID, h.ORDER_NUMBER, h.SHIPPING_METHOD_CODE, h.FLOW_STATUS_CODE,
         h.CANCELLED_FLAG, h.ATTRIBUTE5 AS cancelled_by, s.vendor_id
  FROM OE_ORDER_HEADERS_ALL h
  JOIN storefront s ON s.ORGANIZATION_ID = h.SHIP_FROM_ORG_ID
  WHERE h.ORDERED_DATE >= '2024-01-01' AND h.ORDERED_DATE < '2025-01-01'
)"""

#: The books by quarter of creation, so that each query stays under BigQuery's scan cap (the
#: journals are partitioned by month of creation). A cancellation is booked the night after
#: it happens, so the last window runs into January 2025.
QUARTERS = [("2024-01-01", "2024-04-01"), ("2024-04-01", "2024-07-01"),
            ("2024-07-01", "2024-10-01"), ("2024-10-01", "2025-02-01")]


def _vendors(prompt: str) -> list[int]:
    """The storefronts the prompt lists: the VENDOR_IDs after 'vendor IDs:'."""
    listed = re.search(r"vendor IDs:\s*([\d,\s]+)", prompt).group(1)
    return [int(v) for v in re.findall(r"\d+", listed)]


def steps(prompt: str) -> list:
    orders = _ORDERS.format(ids=", ".join(f"'{v}'" for v in _vendors(prompt)))
    return [
        # 1. How the listed storefronts' orders ended, who cancelled, what support decided,
        #    and who funded the discounts on them (over all of them together).
        ("run_sql", {"sql": orders + """,
cases AS (
  SELECT HEADER_ID, MAX(OVERRIDE_CODE) AS support_outcome
  FROM XX_SUPPORT_CASES WHERE CATEGORY_CODE = 'CANCELLATION' GROUP BY HEADER_ID
),
promo AS (
  SELECT HEADER_ID, SUM(CAST(ATTRIBUTE1 AS NUMERIC)) AS platform_share,
         SUM(CAST(ATTRIBUTE2 AS NUMERIC)) AS storefront_share, SUM(ADJUSTED_AMOUNT) AS discount
  FROM OE_PRICE_ADJUSTMENTS WHERE ATTRIBUTE1 IS NOT NULL GROUP BY HEADER_ID
)
SELECT o.SHIPPING_METHOD_CODE, o.FLOW_STATUS_CODE, o.cancelled_by, c.support_outcome,
       COUNT(*) AS orders, COUNT(p.HEADER_ID) AS discounted,
       SUM(p.platform_share) AS platform_share, SUM(p.storefront_share) AS storefront_share,
       SUM(p.discount) AS discount
FROM orders o
LEFT JOIN cases c ON c.HEADER_ID = o.HEADER_ID
LEFT JOIN promo p ON p.HEADER_ID = o.HEADER_ID
GROUP BY 1, 2, 3, 4
ORDER BY 1, 2, 3, 4"""}),

        # 2. The platform's share of the discounts on each storefront's pickup orders that
        #    went through.
        ("run_sql", {"sql": orders + """
SELECT o.vendor_id, COUNT(DISTINCT o.HEADER_ID) AS discounted_pickups,
       SUM(CAST(pa.ATTRIBUTE1 AS NUMERIC)) AS pickup_promo_platform_usd
FROM orders o
JOIN OE_PRICE_ADJUSTMENTS pa ON pa.HEADER_ID = o.HEADER_ID
WHERE o.SHIPPING_METHOD_CODE = 'PICKUP' AND o.CANCELLED_FLAG = 'N'
  AND pa.ATTRIBUTE1 IS NOT NULL
GROUP BY o.vendor_id"""}),

        # 3-6. The compensation: the cancellation expense the books tie to each order the
        #      customer cancelled, one quarter of the books at a time.
        *[("run_sql", {"sql": orders + f"""
SELECT o.vendor_id, COUNT(*) AS compensated_cancellations,
       SUM(x.ENTERED_DR) - SUM(x.ENTERED_CR) AS cancellation_comp_usd
FROM GL_IMPORT_REFERENCES r
JOIN XX_GL_INTERFACE_HIST x ON CAST(x.INTERFACE_LINE_ID AS STRING) = r.REFERENCE_7
JOIN orders o ON CAST(o.ORDER_NUMBER AS STRING) = r.REFERENCE_2
WHERE r.REFERENCE_4 = 'CANCELLATION_EXPENSE' AND o.cancelled_by = 'CUSTOMER'
  AND r.CREATION_DATE >= '{lo}' AND r.CREATION_DATE < '{hi}'
  AND x.DATE_CREATED >= '{lo}' AND x.DATE_CREATED < '{hi}'
GROUP BY o.vendor_id"""}) for lo, hi in QUARTERS],

        # 7. The five-star reviews of each storefront's 2024 orders.
        ("run_sql", {"sql": orders + """
SELECT o.vendor_id, COUNT(*) AS five_star_reviews
FROM XX_REVIEWS r
JOIN orders o ON o.HEADER_ID = r.HEADER_ID
WHERE r.STARS = 5
GROUP BY o.vendor_id"""}),

        # 8. One row per listed storefront, zero where it had none, money to the cent.
        ("run_python", {"code": f"""\
import pandas as pd
from mission_control import MissionControl

vendors = {_vendors(prompt)!r}

def per_vendor(frames, column):
    df = pd.concat(frames)
    df["vendor_id"] = df["vendor_id"].astype(int)
    return df.groupby("vendor_id")[column].sum().astype(float)

promo = per_vendor([pd.read_parquet("results/sql_0002.parquet")], "pickup_promo_platform_usd")
comp = per_vendor([pd.read_parquet(f"results/sql_{{n:04d}}.parquet") for n in range(3, 7)],
                  "cancellation_comp_usd")
reviews = per_vendor([pd.read_parquet("results/sql_0007.parquet")], "five_star_reviews")

df = pd.DataFrame({{"vendor_id": vendors}})
df["pickup_promo_platform_usd"] = df["vendor_id"].map(promo).fillna(0).round(2)
df["cancellation_comp_usd"] = df["vendor_id"].map(comp).fillna(0).round(2)
df["five_star_reviews"] = df["vendor_id"].map(reviews).fillna(0).astype(int)
print(f"{{len(df)}} storefronts; totals:")
print(df.drop(columns="vendor_id").sum().round(2).to_string())

mission_control = MissionControl()
mission_control.file_schedules([{{
    "name": "platform_funded", "key_columns": ["vendor_id"],
    "rows": df.to_dict("records"),
}}])
mission_control.summary()
"""}),
    ]
