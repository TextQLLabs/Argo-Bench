"""Reference solution for rng-01-bust-out-holds-sep.

The warehouse is current to the end of September. Trust & safety wants payouts held on the
storefronts laundering stolen cards right now. The acquirer's rule describes the scheme: the
storefront's ticket size and volume ramp on cards its customers have never used before, it
keeps getting paid on schedule, and it goes dark before the chargebacks land. The chargebacks
are not the way in. They lag the order by weeks, and hundreds of honest storefronts are
themselves hit by stolen cards. What stands out is the card. On an account that has been
open for a while, a card added within the hour before the payment is almost unheard of at an
honest storefront. Brand-new accounts that add a card and order at once are ordinary
sign-ups. A laundering storefront takes such orders by the dozen, at a multiple of its usual
ticket. The trap is the storefronts where the same scheme has already run its course. Some
stopped trading weeks ago and are no longer paid. Others were still paid in late September
for their last weeks but took no orders on the last day. They have gone dark, and a hold
catches nothing.

The rule filed, over the last 30 days of the warehouse. A fresh-card order is a payment
approved on a card added to the customer's account at most an hour before, on an account
open at least 7 days. A storefront is held when all of these hold: at least ten fresh-card
orders, making up at least 5% of its orders; a larger ticket on those orders than on its
other orders; a payout released in the last two weeks with no hold open on its invoices;
and orders still coming in on the warehouse's last day. Payments are in
``XX_PAYMENT_AUTHS``, the card and the day it was added in ``XX_PAYMENT_INSTRUMENTS``, the
account's opening in ``HZ_CUST_ACCOUNTS``, and the order's storefront in
``OE_ORDER_HEADERS_ALL.SHIP_FROM_ORG_ID``. The storefront's payables ``VENDOR_ID`` is in
``HR_ORGANIZATION_INFORMATION`` (context ``XX_STOREFRONT``), and its payouts are in
``AP_CHECKS_ALL`` and ``AP_HOLDS_ALL``. Every step before the last returns counts only. The
last step returns only the storefronts held.

Score: 1.0 on the paper's warehouse and 0.999 on the released warehouse. Every laundering
storefront is held and nothing else is. The shortfall is the fixed cost of the two reviews.
"""

QUESTION = "rng-01-bust-out-holds-sep"

#: Date arithmetic per dialect: the account's age and the card's age at the payment, and a
#: day some number of days before the warehouse's last day.
_DIALECT = {
    "bigquery": {
        "acct_days": "DATETIME_DIFF(a.AUTH_DATE, c.CREATION_DATE, DAY)",
        "card_minutes": "DATETIME_DIFF(a.AUTH_DATE, p.ADDED_DATE, MINUTE)",
        "days_before": "DATE_SUB(k.today, INTERVAL {n} DAY)",
        "vendor_id": "SAFE_CAST(i.ORG_INFORMATION1 AS INT64)",
    },
    "duckdb": {
        "acct_days": "date_diff('day', c.CREATION_DATE, a.AUTH_DATE)",
        "card_minutes": "date_diff('minute', p.ADDED_DATE, a.AUTH_DATE)",
        "days_before": "CAST(k.today - INTERVAL {n} DAY AS DATE)",
        "vendor_id": "TRY_CAST(i.ORG_INFORMATION1 AS BIGINT)",
    },
}

#: Every approved payment, with its storefront, the age of the customer's account and how
#: long before the payment its card was added.
_PAYMENTS = """\
payments AS (
  SELECT h.SHIP_FROM_ORG_ID AS storefront, a.AUTH_DATE, a.AMOUNT,
         {acct_days} AS account_age_days,
         CASE WHEN a.AUTH_DATE >= p.ADDED_DATE THEN {card_minutes} END AS card_age_minutes
  FROM XX_PAYMENT_AUTHS a
  JOIN XX_PAYMENT_INSTRUMENTS p ON p.PAYMENT_INSTRUMENT_ID = a.PAYMENT_INSTRUMENT_ID
  JOIN OE_ORDER_HEADERS_ALL h ON h.HEADER_ID = a.HEADER_ID
  JOIN HZ_CUST_ACCOUNTS c ON c.CUST_ACCOUNT_ID = h.SOLD_TO_ORG_ID
  WHERE a.STATUS_CODE = 'APPROVED'
)"""

#: Each storefront's last 30 days: its orders, its fresh-card orders (a card added at most
#: an hour before the payment, on an account open at least a week) and their tickets, and
#: the last day it took an order.
_STOREFRONTS = _PAYMENTS + """,
clock AS (
  SELECT CAST(MAX(AUTH_DATE) AS DATE) AS today FROM payments
),
recent AS (
  SELECT y.storefront, y.AMOUNT, CAST(y.AUTH_DATE AS DATE) AS day,
         COALESCE(y.account_age_days >= 7 AND y.card_age_minutes <= 60, FALSE) AS fresh_card
  FROM payments y CROSS JOIN clock k
  WHERE CAST(y.AUTH_DATE AS DATE) > {days_before_30}
),
storefronts AS (
  SELECT r.storefront, COUNT(*) AS orders,
         COUNT(CASE WHEN r.fresh_card THEN 1 END) AS fresh_card_orders,
         AVG(CASE WHEN r.fresh_card THEN r.AMOUNT END) AS fresh_card_ticket,
         AVG(CASE WHEN NOT r.fresh_card THEN r.AMOUNT END) AS other_ticket,
         MAX(r.day) AS last_order_day
  FROM recent r
  GROUP BY r.storefront
),
features AS (
  SELECT s.*, {vendor_id} AS VENDOR_ID,
         s.fresh_card_orders * 1.0 / s.orders AS fresh_card_share,
         s.last_order_day = k.today AS trading_today
  FROM storefronts s
  CROSS JOIN clock k
  JOIN HR_ORGANIZATION_INFORMATION i
    ON i.ORGANIZATION_ID = s.storefront AND i.ORG_INFORMATION_CONTEXT = 'XX_STOREFRONT'
),
payouts AS (
  SELECT ch.VENDOR_ID, MAX(CAST(ch.CHECK_DATE AS DATE)) AS last_payout_day
  FROM AP_CHECKS_ALL ch CROSS JOIN clock k
  WHERE CAST(ch.CHECK_DATE AS DATE) <= k.today
  GROUP BY ch.VENDOR_ID
),
open_holds AS (
  SELECT inv.VENDOR_ID, COUNT(*) AS open_holds
  FROM AP_HOLDS_ALL hd JOIN AP_INVOICES_ALL inv ON inv.INVOICE_ID = hd.INVOICE_ID
  WHERE hd.RELEASE_LOOKUP_CODE IS NULL
  GROUP BY inv.VENDOR_ID
),
-- The acquirer's tells: paid on schedule (a payout in the last two weeks covers weekly and
-- fortnightly payees, and nothing is held yet), and a ramp on fresh cards (by the dozen, a
-- real share of the storefront's orders, at a bigger ticket than its other orders).
rule AS (
  SELECT f.*,
         COALESCE(pay.last_payout_day >= {days_before_14}, FALSE)
           AND COALESCE(oh.open_holds, 0) = 0 AS paid_on_schedule,
         f.fresh_card_orders >= 10 AND f.fresh_card_share >= 0.05
           AND f.fresh_card_ticket > f.other_ticket AS fresh_card_ramp
  FROM features f
  CROSS JOIN clock k
  LEFT JOIN payouts pay ON pay.VENDOR_ID = f.VENDOR_ID
  LEFT JOIN open_holds oh ON oh.VENDOR_ID = f.VENDOR_ID
)"""


def _sql(template: str, select: str) -> dict:
    out = {}
    for engine, d in _DIALECT.items():
        head = template.format(acct_days=d["acct_days"], card_minutes=d["card_minutes"],
                               vendor_id=d["vendor_id"],
                               days_before_30=d["days_before"].format(n=30),
                               days_before_14=d["days_before"].format(n=14))
        out[engine] = "WITH " + head + "\n" + select
    return out


STEPS = [
    # 1. How long before an approved payment was its card added to the customer's account?
    #    Split brand-new accounts from established ones (open a week or more). New accounts
    #    adding a card and paying at once is the ordinary sign-up. On established accounts
    #    a small, high-ticket cluster sits inside the first hour, on cards the customer had
    #    never used before.
    ("run_sql", {"sql": _sql(_PAYMENTS, """\
SELECT CASE WHEN account_age_days >= 7 THEN 'open a week or more' ELSE 'opened this week' END
         AS account,
       CASE WHEN card_age_minutes <= 15 THEN '1. within 15 min'
            WHEN card_age_minutes <= 60 THEN '2. 15-60 min'
            WHEN card_age_minutes <= 120 THEN '3. 1-2 h'
            WHEN card_age_minutes <= 360 THEN '4. 2-6 h'
            ELSE '5. 6-48 h' END AS card_added_before_payment,
       COUNT(*) AS payments, ROUND(AVG(AMOUNT), 2) AS avg_ticket
FROM payments
WHERE card_age_minutes <= 2880
GROUP BY 1, 2
ORDER BY 1, 2""")}),

    # 2. Storefronts by how many fresh-card orders they took in the last 30 days. Honest ones
    #    take one or two at most. At a tiny storefront even one can be 5% of its orders, so
    #    the rule needs a count as well as a share. A laundering one takes them by the dozen.
    #    Of those, which are still paid on schedule, and which still took orders on the last
    #    day? A storefront that ramped and then went dark is past holding. Counts only.
    ("run_sql", {"sql": _sql(_STOREFRONTS, """\
SELECT CASE WHEN fresh_card_orders = 0 THEN '0' WHEN fresh_card_orders <= 2 THEN '1-2'
            WHEN fresh_card_orders <= 9 THEN '3-9' ELSE '10+' END AS fresh_card_orders_30d,
       COUNT(*) AS storefronts,
       ROUND(MAX(fresh_card_share), 4) AS max_fresh_card_share,
       COUNT(CASE WHEN fresh_card_ramp THEN 1 END) AS ramping,
       COUNT(CASE WHEN fresh_card_ramp AND paid_on_schedule THEN 1 END) AS ramping_paid_on_schedule,
       COUNT(CASE WHEN fresh_card_ramp AND trading_today THEN 1 END) AS ramping_trading_today,
       COUNT(CASE WHEN fresh_card_ramp AND paid_on_schedule AND trading_today THEN 1 END)
         AS ramping_paid_and_trading
FROM rule
GROUP BY 1
ORDER BY MIN(fresh_card_orders)""")}),

    # 3. The storefronts to hold: ramping on fresh cards, still paid on schedule, still
    #    trading on the last day.
    ("run_sql", {"sql": _sql(_STOREFRONTS, """\
SELECT VENDOR_ID, orders, fresh_card_orders, ROUND(fresh_card_share, 3) AS fresh_card_share,
       ROUND(fresh_card_ticket / other_ticket, 2) AS ticket_multiple, last_order_day
FROM rule
WHERE fresh_card_ramp AND paid_on_schedule AND trading_today
ORDER BY VENDOR_ID""")}),

    # 4. Hold their payouts for transaction laundering.
    ("run_python", {"code": """\
import pandas as pd
from mission_control import MissionControl, Reason

hold = pd.read_parquet("results/sql_0003.parquet")
print(f"{len(hold)} storefronts to hold: {int(hold['fresh_card_orders'].sum())} fresh-card "
      f"orders in the last 30 days, {hold['fresh_card_share'].min():.0%}-"
      f"{hold['fresh_card_share'].max():.0%} of their orders, at "
      f"{hold['ticket_multiple'].min():.1f}-{hold['ticket_multiple'].max():.1f}x their usual ticket")

mission_control = MissionControl()
mission_control.hold_payouts([int(v) for v in hold["VENDOR_ID"]],
                             reason=Reason.TRANSACTION_LAUNDERING)
mission_control.summary()
"""}),
]
