"""Reference solution for dhc-01-closure-order-sales.

The Health Department asks which storefronts sold while one of its 2024 closure orders was
in force. Nothing in the warehouse flags a breach: the public record sits in
``XX_MERCHANT_REGULATORY_EVENTS`` (``CLOSURE_ORDER`` and ``REOPENING`` rows by the storefront's
``VENDOR_ID``), and the breach shows only when the orders are set against it. The platform's
own pause log (``XX_MERCHANT_CLOSURES``) is not the order and says nothing about it. Three
traps: a closure with no reopening inspection yet is still in force at the end of October
(an inner join to the reopenings drops it); a storefront can be served a second closure
order before it reopens (a re-inspection that keeps it shut), and the order runs from the
FIRST of them; and pairing orders with every closure row counts an order more than once.

The rule filed: a closure order stands from its date until the day of the next reopening
inspection (the storefront may trade that day), or to the end of October if there is none
yet. Orders are counted by the day they were placed (``ORDERED_DATE``), cancelled ones
(``CANCELLED_FLAG = 'Y'``) left out, and reach the storefront through
``HR_ORGANIZATION_INFORMATION`` (context ``XX_STOREFRONT``: ``ORGANIZATION_ID`` is the order's
``SHIP_FROM_ORG_ID``, ``ORG_INFORMATION1`` the ``VENDOR_ID``). One ``report_metrics`` item per
storefront that sold under an order, named by its ``VENDOR_ID``; complying storefronts are
not filed.

Score: 1.0 on the paper's warehouse and 1.0 on the released warehouse.
"""

QUESTION = "dhc-01-closure-order-sales"

#: Each closure order's span. The events are read in date order per storefront; a closure
#: order whose previous event was not itself a closure opens a run, and the run lasts until
#: the next reopening inspection (exclusive), however many closure orders stack up inside it.
_SPANS = """\
WITH ev AS (
  SELECT VENDOR_ID, EVENT_DATE, EVENT_TYPE,
         LAG(EVENT_TYPE) OVER (PARTITION BY VENDOR_ID ORDER BY EVENT_DATE) AS prev_type
  FROM XX_MERCHANT_REGULATORY_EVENTS
  WHERE EVENT_TYPE IN ('CLOSURE_ORDER', 'REOPENING')
),
runs AS (
  SELECT VENDOR_ID, EVENT_DATE, EVENT_TYPE,
         SUM(CASE WHEN EVENT_TYPE = 'CLOSURE_ORDER'
                   AND (prev_type IS NULL OR prev_type <> 'CLOSURE_ORDER') THEN 1 ELSE 0 END)
           OVER (PARTITION BY VENDOR_ID ORDER BY EVENT_DATE
                 ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS run_no
  FROM ev
),
spans AS (
  SELECT VENDOR_ID, run_no,
         MIN(CASE WHEN EVENT_TYPE = 'CLOSURE_ORDER' THEN EVENT_DATE END) AS closed_on,
         MIN(CASE WHEN EVENT_TYPE = 'REOPENING' THEN EVENT_DATE END) AS reopened_on,
         COUNT(CASE WHEN EVENT_TYPE = 'CLOSURE_ORDER' THEN 1 END) AS closure_orders
  FROM runs
  WHERE run_no > 0
  GROUP BY VENDOR_ID, run_no
  HAVING MIN(CASE WHEN EVENT_TYPE = 'CLOSURE_ORDER' THEN EVENT_DATE END) >= DATE '2024-01-01'
)"""

STEPS = [
    # 1. The public record: what kinds of events it holds, over which dates, for how many
    #    storefronts.
    ("run_sql", {"sql": """\
SELECT EVENT_TYPE, COUNT(*) AS events, COUNT(DISTINCT VENDOR_ID) AS storefronts,
       MIN(EVENT_DATE) AS first_event, MAX(EVENT_DATE) AS last_event
FROM XX_MERCHANT_REGULATORY_EVENTS
GROUP BY EVENT_TYPE
ORDER BY EVENT_TYPE"""}),

    # 2. How the closure orders run: a closure order stands until the next reopening
    #    inspection, several can stack up before one reopening, and some have none yet.
    ("run_sql", {"sql": _SPANS + """
SELECT CASE WHEN reopened_on IS NULL THEN 'not reopened yet' ELSE 'reopened' END AS status,
       closure_orders, COUNT(*) AS spans, COUNT(DISTINCT VENDOR_ID) AS storefronts
FROM spans
GROUP BY 1, 2
ORDER BY 1, 2"""}),

    # 3. Orders placed while an order stood, not cancelled, by storefront. Only storefronts
    #    that sold under an order come back.
    ("run_sql", {"sql": _SPANS + """,
stores AS (
  SELECT ORGANIZATION_ID, ORG_INFORMATION1 AS vendor_key
  FROM HR_ORGANIZATION_INFORMATION WHERE ORG_INFORMATION_CONTEXT = 'XX_STOREFRONT'
)
SELECT s.VENDOR_ID, COUNT(DISTINCT h.HEADER_ID) AS orders_under_closure,
       MIN(CAST(h.ORDERED_DATE AS DATE)) AS first_order_day,
       MAX(CAST(h.ORDERED_DATE AS DATE)) AS last_order_day
FROM spans s
JOIN stores st ON st.vendor_key = CAST(s.VENDOR_ID AS STRING)
JOIN OE_ORDER_HEADERS_ALL h ON h.SHIP_FROM_ORG_ID = st.ORGANIZATION_ID
WHERE h.CANCELLED_FLAG = 'N'
  AND h.ORDERED_DATE >= '2024-01-01'
  AND CAST(h.ORDERED_DATE AS DATE) >= s.closed_on
  AND (s.reopened_on IS NULL OR CAST(h.ORDERED_DATE AS DATE) < s.reopened_on)
GROUP BY s.VENDOR_ID
ORDER BY s.VENDOR_ID"""}),

    # 4. One metric per storefront that sold under a closure order, named by its VENDOR_ID.
    ("run_python", {"code": """\
import pandas as pd
from mission_control import MissionControl

sold = pd.read_parquet("results/sql_0003.parquet")
print(f"{len(sold)} storefronts sold under a closure order, "
      f"{int(sold['orders_under_closure'].sum()):,} orders between them")

mission_control = MissionControl()
mission_control.report_metrics(
    [{"name": str(int(r.VENDOR_ID)), "value": int(r.orders_under_closure)}
     for r in sold.itertuples()], unit="count")
mission_control.summary()
"""}),
]
