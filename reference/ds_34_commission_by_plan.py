"""Reference solution for ds-34-commission-by-plan.

The question asks what the platform charged storefronts in commission on each month's
orders of 2024, by the storefront's plan and by component. No table books the four
components against an order: the merchant payout invoices carry them per payout run, on the
payout clock, not the order's. What the books carry per order is what the commission is
charged on and whether the order was charged at all; the plan's rate card does the rest.
Three things the prompt leaves unsaid: where the rates are, which plan a storefront was on in
a given month (its supplier record shows today's plan), and which orders were never charged.

The rate card is the ``MERCHANT COMMISSION PLANS`` price list: each ``QP_LIST_LINES`` row is
one component (``ATTRIBUTE1``) at ``OPERAND`` percent, for the plan and channel its
``QP_PRICING_ATTRIBUTES`` name (``PRICING_ATTRIBUTE3`` = plan, ``PRICING_ATTRIBUTE4`` =
DELIVERY or PICKUP); a component a plan does not list on a channel is zero. The rate applies
to the order's menu items, its STANDARD lines in ``OE_ORDER_LINES_ALL`` (quantity times
selling price; the SERVICE lines are fees and tips). An order was charged when it has a
customer invoice (``RA_CUSTOMER_TRX_ALL``, type INV, ``INTERFACE_HEADER_ATTRIBUTE1`` = order
number); a cancelled order that was never invoiced earns nothing but still counts. The plan
is ``AP_SUPPLIERS.ATTRIBUTE1`` unless the audit trail (``XX_AUDIT_TRAIL``, column
ATTRIBUTE1) records a change: each change takes effect on the first of the month it was
entered in (a weekend first is entered the next business day), the old plan before it.
Orders reach their storefront through ``HR_ORGANIZATION_INFORMATION`` (``XX_STOREFRONT``).
Orders by ``ORDERED_DATE``, every status, a quarter of the year per query.

Score: 1.0 on the paper's warehouse and 1.0 on the released warehouse.
"""

QUESTION = "ds-34-commission-by-plan"

_RATES = """\
SELECT plan, channel,
       SUM(CASE WHEN component = 'DELIVERY' THEN rate ELSE 0 END) AS delivery,
       SUM(CASE WHEN component = 'MARKETING' THEN rate ELSE 0 END) AS marketing,
       SUM(CASE WHEN component = 'PAYMENT_PROCESSING' THEN rate ELSE 0 END) AS processing,
       SUM(CASE WHEN component = 'OTHER_SERVICES' THEN rate ELSE 0 END) AS other
FROM (
  SELECT l.LIST_LINE_ID, l.ATTRIBUTE1 AS component, l.OPERAND / 100 AS rate,
         MAX(CASE WHEN a.PRICING_ATTRIBUTE = 'PRICING_ATTRIBUTE3'
                  THEN a.PRICING_ATTR_VALUE_FROM END) AS plan,
         MAX(CASE WHEN a.PRICING_ATTRIBUTE = 'PRICING_ATTRIBUTE4'
                  THEN a.PRICING_ATTR_VALUE_FROM END) AS channel
  FROM QP_LIST_HEADERS_TL h
  JOIN QP_LIST_LINES l ON l.LIST_HEADER_ID = h.LIST_HEADER_ID
  JOIN QP_PRICING_ATTRIBUTES a ON a.LIST_LINE_ID = l.LIST_LINE_ID
  WHERE h.NAME = 'MERCHANT COMMISSION PLANS'
  GROUP BY 1, 2, 3
) lines
GROUP BY plan, channel"""

_COMMISSION = """\
WITH rates AS (
{rates}
), changes AS (
  -- Each plan change takes effect from the first of the month it was entered in.
  SELECT CAST(SOURCE_KEY_VALUE AS INT64) AS vendor_id, OLD_VALUE, NEW_VALUE,
         EXTRACT(YEAR FROM CHANGED_DATE) * 100 + EXTRACT(MONTH FROM CHANGED_DATE) AS from_month,
         LEAD(EXTRACT(YEAR FROM CHANGED_DATE) * 100 + EXTRACT(MONTH FROM CHANGED_DATE))
           OVER (PARTITION BY SOURCE_KEY_VALUE ORDER BY CHANGED_DATE, AUDIT_ID) AS next_month,
         ROW_NUMBER() OVER (PARTITION BY SOURCE_KEY_VALUE ORDER BY CHANGED_DATE, AUDIT_ID) AS k
  FROM XX_AUDIT_TRAIL
  WHERE SOURCE_TABLE = 'AP_SUPPLIERS' AND COLUMN_NAME = 'ATTRIBUTE1'
), periods AS (
  -- The plan from each change until the next one, and the old plan before the first.
  SELECT vendor_id, from_month, COALESCE(next_month, 999999) AS to_month, NEW_VALUE AS plan
  FROM changes
  UNION ALL
  SELECT vendor_id, 0 AS from_month, from_month AS to_month, OLD_VALUE AS plan
  FROM changes WHERE k = 1
), orders AS (
  SELECT h.HEADER_ID, CAST(h.ORDER_NUMBER AS STRING) AS order_ref,
         EXTRACT(YEAR FROM h.ORDERED_DATE) * 100 + EXTRACT(MONTH FROM h.ORDERED_DATE) AS month_key,
         h.SHIPPING_METHOD_CODE AS channel, CAST(oi.ORG_INFORMATION1 AS INT64) AS vendor_id
  FROM OE_ORDER_HEADERS_ALL h
  LEFT JOIN HR_ORGANIZATION_INFORMATION oi
    ON oi.ORGANIZATION_ID = h.SHIP_FROM_ORG_ID AND oi.ORG_INFORMATION_CONTEXT = 'XX_STOREFRONT'
  WHERE h.ORDERED_DATE >= '{lo}' AND h.ORDERED_DATE < '{hi}'
    AND h.CREATION_DATE >= {lo_pad} AND h.CREATION_DATE < {hi_pad}
), food AS (
  -- What the commission is charged on: the order's menu items.
  SELECT HEADER_ID, SUM(ORDERED_QUANTITY * UNIT_SELLING_PRICE) AS food_usd
  FROM OE_ORDER_LINES_ALL
  WHERE ITEM_TYPE_CODE = 'STANDARD'
    AND CREATION_DATE >= {lo_pad} AND CREATION_DATE < {hi_pad}
  GROUP BY HEADER_ID
), charged AS (
  -- Orders the customer was invoiced for; the rest were never charged.
  SELECT DISTINCT t.INTERFACE_HEADER_ATTRIBUTE1 AS order_ref
  FROM RA_CUSTOMER_TRX_ALL t
  JOIN RA_CUST_TRX_TYPES_ALL y ON y.CUST_TRX_TYPE_ID = t.CUST_TRX_TYPE_ID
  WHERE y.TYPE = 'INV' AND t.INTERFACE_HEADER_CONTEXT = 'ORDER ENTRY'
    AND t.CREATION_DATE >= {lo_pad} AND t.CREATION_DATE < {hi_pad}
), priced AS (
  SELECT o.month_key, o.channel, COALESCE(p.plan, s.ATTRIBUTE1) AS plan,
         CASE WHEN c.order_ref IS NOT NULL THEN COALESCE(f.food_usd, 0) ELSE 0 END AS base_usd,
         CASE WHEN c.order_ref IS NOT NULL THEN 1 ELSE 0 END AS charged
  FROM orders o
  LEFT JOIN food f ON f.HEADER_ID = o.HEADER_ID
  LEFT JOIN charged c ON c.order_ref = o.order_ref
  LEFT JOIN AP_SUPPLIERS s ON s.VENDOR_ID = o.vendor_id
  LEFT JOIN periods p
    ON p.vendor_id = o.vendor_id AND o.month_key >= p.from_month AND o.month_key < p.to_month
)
SELECT o.month_key, o.plan, COUNT(*) AS orders, SUM(o.charged) AS charged_orders,
       SUM(o.base_usd) AS base_usd,
       SUM(o.base_usd * COALESCE(r.delivery, 0)) AS commission_delivery_usd,
       SUM(o.base_usd * COALESCE(r.marketing, 0)) AS commission_marketing_usd,
       SUM(o.base_usd * COALESCE(r.processing, 0)) AS commission_processing_usd,
       SUM(o.base_usd * COALESCE(r.other, 0)) AS commission_other_usd
FROM priced o
LEFT JOIN rates r ON r.plan = o.plan AND r.channel = o.channel
GROUP BY o.month_key, o.plan"""

#: A quarter of orders per query keeps each under BigQuery's 20 GiB scan cap (the tables are
#: partitioned by month of creation). Lines are written with the order; the customer invoice
#: follows within days. The creation windows are padded by a month on both sides all the same.
QUARTERS = [("2024-01-01", "2024-04-01"), ("2024-04-01", "2024-07-01"),
            ("2024-07-01", "2024-10-01"), ("2024-10-01", "2025-01-01")]

_PAD = {"bigquery": ("DATE_SUB(DATE '{lo}', INTERVAL 1 MONTH)",
                     "DATE_ADD(DATE '{hi}', INTERVAL 1 MONTH)"),
        "duckdb": ("DATE '{lo}' - INTERVAL 1 MONTH", "DATE '{hi}' + INTERVAL 1 MONTH")}


def _quarter(lo: str, hi: str) -> tuple[str, dict]:
    return ("run_sql", {"sql": {
        engine: _COMMISSION.format(rates=_RATES, lo=lo, hi=hi,
                                   lo_pad=pad[0].format(lo=lo), hi_pad=pad[1].format(hi=hi))
        for engine, pad in _PAD.items()}})


STEPS = [
    # 1. The rate card: which component each plan charges on each channel, and at what rate.
    ("run_sql", {"sql": _RATES + "\nORDER BY plan, channel"}),

    # 2. Plan changes on the storefronts' supplier records: when they were entered and from
    #    which plan to which. Every one is entered on the first business day of a month.
    ("run_sql", {"sql": """\
SELECT EXTRACT(YEAR FROM CHANGED_DATE) * 100 + EXTRACT(MONTH FROM CHANGED_DATE) AS month_key,
       EXTRACT(DAY FROM CHANGED_DATE) AS day_entered, OLD_VALUE AS old_plan,
       NEW_VALUE AS new_plan, COUNT(*) AS changes,
       COUNT(DISTINCT SOURCE_KEY_VALUE) AS storefronts
FROM XX_AUDIT_TRAIL
WHERE SOURCE_TABLE = 'AP_SUPPLIERS' AND COLUMN_NAME = 'ATTRIBUTE1'
GROUP BY 1, 2, 3, 4
ORDER BY 1, 3"""}),

    # 3-6. Orders, charged base and commission by month and plan, a quarter at a time.
    *[_quarter(lo, hi) for lo, hi in QUARTERS],

    # 7. Shape to the contract and publish.
    ("run_python", {"code": """\
import pandas as pd
from mission_control import MissionControl

df = pd.concat([pd.read_parquet(f"results/sql_{n:04d}.parquet") for n in range(3, 7)])
assert df["plan"].notna().all(), "every order resolves to a plan"
money = ["base_usd", "commission_delivery_usd", "commission_marketing_usd",
         "commission_processing_usd", "commission_other_usd"]
df[money] = df[money].astype(float)
df["month"] = df["month_key"].astype(int).map(lambda k: f"{k // 100}-{k % 100:02d}")
df["plan"] = df["plan"].str.lower()
df = df[df["month"].str.startswith("2024-")]
df = df.sort_values(["month", "plan"]).reset_index(drop=True)
print(df[["month", "plan", "orders", "charged_orders", "base_usd"]].to_string(index=False))

out = df[["month", "plan", "orders", *money[1:]]].copy()
out["orders"] = out["orders"].astype(int)
out[money[1:]] = out[money[1:]].round(2)

mission_control = MissionControl()
mission_control.publish_data_sources([
    {"name": "storefront_commission_monthly_by_plan_2024", "frame": out},
])
mission_control.summary()
"""}),
]
